"""核心任务可靠性的纯 fake/mock 单元测试。

这些测试不会启动 Chromium、访问抖音或读取真实账号配置；所有页面、响应和浏览器
对象均为内存中的最小替身。
"""

import importlib
import unittest
from unittest.mock import patch

import core.tasks as tasks


TEST_CONFIG = {
    "browserTimeout": 12_000,
    "friendListTimeout": 750,
    "taskRetryTimes": 1,
    "logLevel": "INFO",
    "blockBrowserResources": True,
}


class DelayedResponse:
    """第一次读取失败、第二次返回数据，用于模拟响应体延迟。"""

    url = "https://www.douyin.com/aweme/v1/web/im/user/info"

    def __init__(self):
        self.calls = 0

    def json(self):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("响应体尚未就绪")
        return {
            "data": [
                {
                    "short_id": "短号",
                    "unique_id": "账号一标识",
                    "sec_uid": "安全标识一",
                    "nickname": "同名好友",
                    "remark_name": "   ",
                }
            ]
        }


class IdentityIndexThatBecomesAmbiguous(dict):
    """在第二次读取时注入同名身份，模拟计划后到点击前的 response 竞态。"""

    def __init__(self, display_name, initial_identity, late_identity):
        super().__init__({display_name: {initial_identity}})
        self.display_name = display_name
        self.late_identity = late_identity
        self.read_count = 0

    def get(self, key, default=None):
        if key == self.display_name:
            self.read_count += 1
            if self.read_count == 2:
                # 第一次读取发生在全局计划构建，第二次正是点击前重验。集合在此变
                # 为两个不同身份，生产代码必须取消点击而不是沿用旧计划。
                super().__getitem__(key).add(self.late_identity)
        return super().get(key, default)


class FakeChatInput:
    """模拟唯一 Slate 编辑器，并可控制 Enter 后是否清空。"""

    class SlateNodes:
        """分别模拟结构 leaf 与仅包含真实字符的 Slate string 节点。"""

        def __init__(self, editor, node_kind):
            self.editor = editor
            self.node_kind = node_kind

        def count(self):
            return 1 if self.node_kind == "leaf" else len(self.all_inner_texts())

        def all_inner_texts(self):
            if self.node_kind == "leaf":
                # 典型 Slate 空态会把 placeholder 嵌在 leaf 内；故意返回占位文案，
                # 证明受测代码没有通过 leaf.innerText 判断草稿。
                return [f"{self.editor.placeholder_text}{self.editor.text}"]
            # 空 Slate 使用 zero-width 节点而不是 data-slate-string；fake 明确建模
            # 这一差异，防止生产代码把 BOM 当作真实用户草稿。
            visible_text = self.editor.text.replace("\ufeff", "").replace("\u200b", "")
            return [visible_text] if visible_text else []

    def __init__(
        self,
        initial_text="",
        clear_after_enter=True,
        element_count=1,
        placeholder_text="",
    ):
        self.actions = []
        self.text = initial_text
        self.clear_after_enter = clear_after_enter
        self.element_count = element_count
        self.placeholder_text = placeholder_text

    def count(self):
        # Playwright Locator 的 count 用于证明页面里只有一个真实可编辑节点。
        return self.element_count

    def inner_text(self):
        # 故意把占位文案计入编辑器整体 innerText，确保生产代码只读取 leaf。
        return self.text or self.placeholder_text

    def locator(self, selector):
        if selector == tasks.SLATE_TEXT_LEAF_SELECTOR:
            return self.SlateNodes(self, "leaf")
        if selector == tasks.SLATE_TEXT_STRING_SELECTOR:
            return self.SlateNodes(self, "string")
        raise AssertionError(f"未预期的 Slate 选择器：{selector}")

    def type(self, value):
        self.actions.append(("type", value))
        self.text += value

    def press(self, value):
        self.actions.append(("press", value))
        if value == "Shift+Enter":
            self.text += "\n"
        elif value == "Enter" and self.clear_after_enter:
            self.text = ""


class FakeTextLocator:
    """提供右侧标题所需的唯一性与文本接口。"""

    def __init__(self, text, count=1):
        self.text = text
        self.element_count = count

    def count(self):
        return self.element_count

    def inner_text(self, timeout=None):
        return self.text


class FakeConversationItem:
    """模拟可在点击后进入当前会话状态的同一个列表项 Locator。"""

    class IndexedAncestor:
        """模拟线上直接父 div 提供的稳定 ``data-index``。"""

        def __init__(self, stable_index):
            self.stable_index = stable_index

        def count(self):
            return 0 if self.stable_index is None else 1

        def get_attribute(self, attribute_name, timeout=None):
            if attribute_name != "data-index":
                raise AssertionError(f"未预期读取索引属性：{attribute_name}")
            return None if self.stable_index is None else str(self.stable_index)

    def __init__(self, display_name, stable_index=0, activate_on_click=True):
        self.display_name = display_name
        self.stable_index = stable_index
        self.activate_on_click = activate_on_click
        self.clicked = False
        self.active = False

    def locator(self, selector):
        if selector == tasks.CONVERSATION_TITLE_SELECTOR:
            return FakeTextLocator(self.display_name)
        if selector == tasks.CONVERSATION_INDEX_ANCESTOR_SELECTOR:
            return self.IndexedAncestor(self.stable_index)
        raise AssertionError(f"未预期的会话项选择器：{selector}")

    def click(self):
        self.clicked = True
        if self.activate_on_click:
            self.active = True

    def get_attribute(self, attribute_name, timeout=None):
        if attribute_name != "class":
            raise AssertionError(f"未预期读取属性：{attribute_name}")
        classes = ["conversationConversationItemwrapper"]
        if self.active:
            classes.append(tasks.CURRENT_CONVERSATION_CLASS)
        return " ".join(classes)


class FakePage:
    """仅实现 ``do_user_task`` 在受测路径会使用的同步页面接口。"""

    def __init__(
        self,
        chat_input=None,
        goto_error=None,
        right_title="确认好友",
    ):
        self.chat_input = chat_input or FakeChatInput()
        self.goto_error = goto_error
        self.right_title = FakeTextLocator(right_title)
        self.events = {}
        self.waited_selectors = []
        self.waited_milliseconds = []
        self.goto_urls = []
        self.goto_options = []

    def on(self, event_name, callback):
        self.events[event_name] = callback

    def goto(self, url, **options):
        self.goto_urls.append(url)
        self.goto_options.append(options)
        if self.goto_error is not None:
            raise self.goto_error

    def wait_for_selector(self, selector, timeout):
        self.waited_selectors.append((selector, timeout))

    def wait_for_timeout(self, milliseconds):
        self.waited_milliseconds.append(milliseconds)

    def locator(self, selector):
        if selector == tasks.CHAT_EDITOR_SELECTOR:
            return self.chat_input
        if selector == tasks.RIGHT_PANEL_TITLE_SELECTOR:
            return self.right_title
        raise AssertionError(f"测试未预期访问选择器：{selector}")


class FakeContext:
    """记录 context 配置与关闭状态，不保存任何真实 Cookie。"""

    def __init__(self, page, close_error=None):
        self.page = page
        self.close_error = close_error
        self.closed = False
        self.routes = []
        self.cookies = None
        self.navigation_timeout = None
        self.default_timeout = None

    def set_default_navigation_timeout(self, value):
        self.navigation_timeout = value

    def set_default_timeout(self, value):
        self.default_timeout = value

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def new_page(self):
        return self.page

    def add_cookies(self, cookies):
        self.cookies = cookies

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeBrowser:
    """为单账号测试返回指定 context，并记录全局浏览器关闭动作。"""

    def __init__(self, context=None, close_error=None):
        self.context = context
        self.close_error = close_error
        self.closed = False

    def new_context(self):
        return self.context

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakePlaywright:
    """记录运行时是否在 browser.close 之后仍得到停止机会。"""

    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeRoute:
    """模拟 Playwright Route/Request 的资源类型分流。"""

    class Request:
        def __init__(self, resource_type):
            self.resource_type = resource_type

    def __init__(self, resource_type):
        self.request = self.Request(resource_type)
        self.action = None

    def abort(self):
        self.action = "abort"

    def continue_(self):
        self.action = "continue"


class DelayedIdentityScrollPage:
    """先展示好友、滚动等待后才填充身份索引的列表页面替身。"""

    class ItemsLocator:
        def __init__(self, elements):
            self.elements = elements

        def all(self):
            return self.elements

    class ScrollLocator:
        def __init__(self, handle):
            self.handle = handle

        def element_handle(self):
            return self.handle

        def count(self):
            return 1

    def __init__(self, identity_index):
        self.identity_index = identity_index
        self.element = FakeConversationItem("延迟好友")
        self.scroll_handle = object()
        self.scroll_top = 0

    def locator(self, selector):
        if selector == tasks.CONVERSATION_ITEM_SELECTOR:
            return self.ItemsLocator([self.element])
        if selector == tasks.CONVERSATION_LIST_SELECTOR:
            return self.ScrollLocator(self.scroll_handle)
        if selector == tasks.RIGHT_PANEL_TITLE_SELECTOR:
            return FakeTextLocator("延迟好友")
        raise AssertionError(f"未预期的列表选择器：{selector}")

    def evaluate(self, expression, ignored_handle):
        # 单页列表从一开始就位于底部；同时支持生产代码的回顶表达式。
        if "scrollTop:" in expression:
            return {"scrollTop": 0, "clientHeight": 100, "scrollHeight": 100}
        if "scrollTop = 0" in expression:
            return 0
        raise AssertionError(f"未预期的列表脚本：{expression}")

    def wait_for_timeout(self, ignored_milliseconds):
        # 模拟 response 回调在首轮列表扫描之后才把昵称关联到配置 unique_id。
        self.identity_index["延迟好友"] = {
            tasks.FriendIdentity(
                short_id="",
                unique_id="延迟标识",
                sec_uid="",
                nickname="延迟好友",
                remark_name="延迟好友",
            )
        }


class FakeConversationListPage:
    """模拟按视口虚拟化的列表，支持预扫描、到底证据和回顶验证。"""

    class ItemsLocator:
        def __init__(self, elements):
            self.elements = elements

        def all(self):
            return self.elements

    class ScrollLocator:
        def __init__(self, handle):
            self.handle = handle

        def element_handle(self):
            return self.handle

        def count(self):
            return 1

    def __init__(self, elements, right_title, *, pages=None):
        # 单页调用保持简洁；多页用 pages 显式模拟后续虚拟页，确保首屏不会看到
        # 后页元素，正好覆盖“先发送、后发现同名”的历史风险。
        self.pages = pages if pages is not None else [elements]
        self.right_title = FakeTextLocator(right_title)
        self.scroll_handle = object()
        self.scroll_top = 0
        self.client_height = 100
        self.waited_milliseconds = []

    @property
    def visible_elements(self):
        page_index = min(
            int(self.scroll_top // self.client_height),
            len(self.pages) - 1,
        )
        return self.pages[page_index]

    def locator(self, selector):
        if selector == tasks.CONVERSATION_ITEM_SELECTOR:
            return self.ItemsLocator(self.visible_elements)
        if selector == tasks.CONVERSATION_LIST_SELECTOR:
            return self.ScrollLocator(self.scroll_handle)
        if selector == tasks.RIGHT_PANEL_TITLE_SELECTOR:
            return self.right_title
        raise AssertionError(f"未预期的列表选择器：{selector}")

    def evaluate(self, expression, ignored_handle):
        scroll_height = self.client_height * len(self.pages)
        if "scrollTop:" in expression:
            return {
                "scrollTop": self.scroll_top,
                "clientHeight": self.client_height,
                "scrollHeight": scroll_height,
            }
        if "scrollTop = 0" in expression:
            self.scroll_top = 0
            return self.scroll_top
        if "Math.min" in expression:
            # 生产代码传入 [handle, step]；fake 只使用步长并模拟浏览器的最大滚动值。
            step = float(ignored_handle[1])
            self.scroll_top = min(
                self.scroll_top + step,
                scroll_height - self.client_height,
            )
            return self.scroll_top
        raise AssertionError(f"未预期的列表脚本：{expression}")

    def wait_for_timeout(self, milliseconds):
        self.waited_milliseconds.append(milliseconds)


class TaskReliabilityTests(unittest.TestCase):
    def test_delayed_response_retries_and_empty_remark_falls_back_per_account(self):
        first_account_index = {}
        second_account_index = {}
        response = DelayedResponse()
        sleep_calls = []

        updated = tasks.handle_response(
            response,
            first_account_index,
            retries=2,
            retry_delay=0.25,
            sleep_fn=sleep_calls.append,
        )

        self.assertTrue(updated)
        self.assertEqual(response.calls, 2)
        self.assertEqual(sleep_calls, [0.25])
        self.assertEqual(len(first_account_index["同名好友"]), 1)
        self.assertEqual(
            next(iter(first_account_index["同名好友"])).remark_name,
            "同名好友",
        )
        self.assertEqual(
            tasks.checkTargetName(
                "同名好友", ["账号一标识"], identity_index=first_account_index
            ),
            "账号一标识",
        )
        # 第二账号未接收该响应，哪怕页面出现同名好友也不得借用第一账号的身份。
        self.assertIsNone(
            tasks.checkTargetName(
                "同名好友", ["账号一标识"], identity_index=second_account_index
            )
        )

    def test_repeated_last_line_still_gets_all_required_newlines(self):
        chat_input = FakeChatInput()

        tasks._type_multiline_message(chat_input, "重复行\\n中间行\\n重复行")

        self.assertEqual(
            chat_input.actions,
            [
                ("type", "重复行"),
                ("press", "Shift+Enter"),
                ("type", "中间行"),
                ("press", "Shift+Enter"),
                ("type", "重复行"),
            ],
        )

    def test_friend_is_rechecked_when_identity_response_arrives_after_first_scan(self):
        identity_index = {}
        page = DelayedIdentityScrollPage(identity_index)

        selected = list(
            tasks.scroll_and_select_user(
                page,
                "延迟账号",
                ["延迟标识"],
                identity_index=identity_index,
                friend_list_wait_time=500,
                confirmation_timeout=100,
            )
        )

        self.assertEqual(
            [selection.target_symbol for selection in selected],
            ["延迟标识"],
        )
        self.assertTrue(page.element.clicked)

    def test_duplicate_identity_mapping_is_preserved_and_never_matches(self):
        """同一显示名对应多个身份时，后到响应不能覆盖并产生伪唯一匹配。"""

        identity_index = {
            "同名好友": {
                tasks.FriendIdentity("", "身份甲", "", "同名好友", "同名好友"),
                tasks.FriendIdentity("", "身份乙", "", "同名好友", "同名好友"),
            }
        }

        self.assertIsNone(
            tasks.checkTargetName(
                "同名好友",
                ["身份乙"],
                identity_index=identity_index,
            )
        )

    def test_identity_becoming_ambiguous_after_plan_aborts_before_click(self):
        """计划后新到同名响应时，点击前重验必须看到歧义并保持零点击。"""

        initial_identity = tasks.FriendIdentity(
            "",
            "目标身份",
            "",
            "竞态好友",
            "竞态好友",
        )
        late_identity = tasks.FriendIdentity(
            "",
            "其他身份",
            "",
            "竞态好友",
            "竞态好友",
        )
        identity_index = IdentityIndexThatBecomesAmbiguous(
            "竞态好友",
            initial_identity,
            late_identity,
        )
        item = FakeConversationItem("竞态好友", stable_index=0)
        page = FakeConversationListPage([item], right_title="竞态好友")

        with self.assertRaises(tasks.ConversationSelectionError):
            list(
                tasks.scroll_and_select_user(
                    page,
                    "身份竞态账号",
                    ["目标身份"],
                    identity_index=identity_index,
                    friend_list_wait_time=1,
                    confirmation_timeout=1,
                )
            )

        self.assertEqual(identity_index.read_count, 2)
        self.assertFalse(item.clicked)

    def test_duplicate_normalized_titles_in_one_round_are_never_clicked(self):
        """全角/空白规范化后重名的两个 DOM 项必须整轮 fail-closed。"""

        first = FakeConversationItem("同名 好友", stable_index=0)
        second = FakeConversationItem("同名　好友", stable_index=1)
        page = FakeConversationListPage([first, second], right_title="同名 好友")

        with self.assertRaises(tasks.ConversationSelectionError):
            list(
                tasks.scroll_and_select_user(
                    page,
                    "歧义账号",
                    ["同名 好友"],
                    identity_index={},
                    friend_list_wait_time=1,
                    confirmation_timeout=1,
                )
            )

        self.assertFalse(first.clicked)
        self.assertFalse(second.clicked)

    def test_same_title_on_later_virtual_page_is_found_before_first_click(self):
        """首屏目标与后续虚拟页同名时，全量预扫描必须在任何点击前取消计划。"""

        first_page_item = FakeConversationItem("跨页同名", stable_index=0)
        later_page_item = FakeConversationItem("跨页同名", stable_index=1)
        page = FakeConversationListPage(
            [],
            right_title="跨页同名",
            pages=[[first_page_item], [later_page_item]],
        )

        with self.assertRaises(tasks.ConversationSelectionError):
            list(
                tasks.scroll_and_select_user(
                    page,
                    "跨页歧义账号",
                    ["跨页同名"],
                    identity_index={},
                    friend_list_wait_time=1,
                    confirmation_timeout=1,
                )
            )

        self.assertFalse(first_page_item.clicked)
        self.assertFalse(later_page_item.clicked)

    def test_missing_stable_index_aborts_inventory_before_click(self):
        """任何会话项缺少稳定 data-index 祖先时，库存都不能被视为完整。"""

        item = FakeConversationItem("无索引好友", stable_index=None)
        page = FakeConversationListPage([item], right_title="无索引好友")

        with self.assertRaises(tasks.ConversationSelectionError):
            list(
                tasks.scroll_and_select_user(
                    page,
                    "无索引账号",
                    ["无索引好友"],
                    identity_index={},
                    friend_list_wait_time=1,
                    confirmation_timeout=1,
                )
            )

        self.assertFalse(item.clicked)

    def test_click_requires_active_item_and_matching_right_title(self):
        """确认成功必须同时来自被点击元素的当前类与规范化后相同的右侧标题。"""

        item = FakeConversationItem("目标 好友")
        page = FakeConversationListPage([item], right_title=" 目标　好友 ")

        selected = list(
            tasks.scroll_and_select_user(
                page,
                "确认账号",
                ["目标 好友"],
                identity_index={},
                friend_list_wait_time=1,
                confirmation_timeout=1,
            )
        )

        self.assertTrue(item.clicked)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].target_symbol, "目标 好友")

    def test_remaining_targets_are_reinventoried_after_list_reorder(self):
        """一次发送导致 data-index 重排后，剩余目标必须用新库存继续且不重复首项。"""

        first = FakeConversationItem("目标甲", stable_index=0)
        second_before_reorder = FakeConversationItem("目标乙", stable_index=1)
        page = FakeConversationListPage(
            [first, second_before_reorder],
            right_title="目标甲",
        )
        selections = tasks.scroll_and_select_user(
            page,
            "重排账号",
            ["目标甲", "目标乙"],
            identity_index={},
            friend_list_wait_time=1,
            confirmation_timeout=1,
        )

        first_selection = next(selections)
        self.assertEqual(first_selection.target_symbol, "目标甲")

        # 模拟第一条发送后会话列表重排：剩余目标移到 index=0，已发送目标变为
        # index=1。生成器恢复后必须重新全量盘点，不能复用上一轮 data-index。
        second_after_reorder = FakeConversationItem("目标乙", stable_index=0)
        first_after_reorder = FakeConversationItem("目标甲", stable_index=1)
        page.pages = [[second_after_reorder, first_after_reorder]]
        page.right_title.text = "目标乙"

        second_selection = next(selections)
        self.assertEqual(second_selection.target_symbol, "目标乙")
        with self.assertRaises(StopIteration):
            next(selections)

        self.assertEqual(first.clicked, True)
        self.assertEqual(second_after_reorder.clicked, True)
        self.assertFalse(second_before_reorder.clicked)
        self.assertFalse(first_after_reorder.clicked)

    def test_confirmation_failure_raises_before_any_editor_input(self):
        """列表项已激活但右侧标题不符时，必须抛错且输入替身保持零动作。"""

        item = FakeConversationItem("目标好友")
        page = FakeConversationListPage([item], right_title="其他好友")
        untouched_editor = FakeChatInput()

        with self.assertRaises(tasks.ConversationSelectionError):
            list(
                tasks.scroll_and_select_user(
                    page,
                    "确认失败账号",
                    ["目标好友"],
                    identity_index={},
                    friend_list_wait_time=1,
                    confirmation_timeout=1,
                )
            )

        self.assertTrue(item.clicked)
        self.assertEqual(untouched_editor.actions, [])

    def test_resource_route_only_blocks_explicit_heavy_types(self):
        for resource_type in ("image", "media", "font"):
            route = FakeRoute(resource_type)
            tasks._handle_lightweight_route(route)
            self.assertEqual(route.action, "abort")

        for resource_type in ("document", "script", "xhr", "fetch", "stylesheet"):
            route = FakeRoute(resource_type)
            tasks._handle_lightweight_route(route)
            self.assertEqual(route.action, "continue")

    def test_account_task_uses_configured_wait_and_returns_unconfirmed_submission(self):
        chat_input = FakeChatInput()
        page = FakePage(chat_input=chat_input, right_title="确认好友")
        context = FakeContext(page)
        browser = FakeBrowser(context)
        selected_item = FakeConversationItem("确认好友")
        selected_item.active = True
        selection = tasks.ConfirmedConversation(
            target_symbol="目标一",
            display_name="确认好友",
            item=selected_item,
        )

        with patch.object(tasks, "scroll_and_select_user", return_value=[selection]), patch.object(
            tasks, "_build_message", return_value="末行\\n中间\\n末行"
        ):
            result = tasks.do_user_task(
                browser,
                "账号一",
                [{"name": "cookie-name", "value": "已脱敏"}],
                ["目标一"],
                runtime_config=TEST_CONFIG,
            )

        self.assertEqual(result.state, tasks.TaskState.SUBMITTED_UNCONFIRMED)
        self.assertEqual(result.submitted_targets, ("目标一",))
        self.assertTrue(context.closed)
        self.assertEqual(context.routes[0][0], "**/*")
        self.assertIn(
            (tasks.CONVERSATION_LIST_SELECTOR, TEST_CONFIG["browserTimeout"]),
            page.waited_selectors,
        )
        # 编辑器立即清空时无需额外固定 sleep；只保留初始列表数据加载等待。
        self.assertEqual(page.waited_milliseconds, [750])
        self.assertEqual(chat_input.actions[-1], ("press", "Enter"))
        self.assertEqual(page.goto_options, [{"wait_until": "domcontentloaded"}])
        self.assertEqual(
            [action for action in chat_input.actions if action == ("press", "Shift+Enter")],
            [("press", "Shift+Enter"), ("press", "Shift+Enter")],
        )

    def test_message_build_state_change_is_rechecked_before_first_type(self):
        """远程消息构建期间标题变化时，返回后必须在零输入状态终止。"""

        chat_input = FakeChatInput()
        page = FakePage(chat_input=chat_input, right_title="确认好友")
        context = FakeContext(page)
        browser = FakeBrowser(context)
        item = FakeConversationItem("确认好友")
        item.active = True
        selection = tasks.ConfirmedConversation("目标一", "确认好友", item)

        def build_message_and_change_conversation():
            # 模拟一言 HTTP 请求阻塞期间右侧会话发生变化；返回后任何 type 都会
            # 写入错误会话，因此必须由新增的构建后复核拦截。
            page.right_title.text = "其他好友"
            return "不应输入的消息"

        with patch.object(
            tasks,
            "scroll_and_select_user",
            return_value=[selection],
        ), patch.object(
            tasks,
            "_build_message",
            side_effect=build_message_and_change_conversation,
        ):
            with self.assertRaises(tasks.ConversationSelectionError):
                tasks.do_user_task(
                    browser,
                    "构建竞态账号",
                    [],
                    ["目标一"],
                    runtime_config={**TEST_CONFIG, "browserTimeout": 1},
                )

        self.assertEqual(chat_input.actions, [])
        self.assertTrue(context.closed)

    def test_existing_draft_aborts_before_typing_or_enter(self):
        """编辑器内已有用户草稿时不能追加模板，更不能按 Enter。"""

        chat_input = FakeChatInput(initial_text="用户尚未发送的旧草稿")
        page = FakePage(chat_input=chat_input, right_title="确认好友")
        context = FakeContext(page)
        browser = FakeBrowser(context)
        item = FakeConversationItem("确认好友")
        item.active = True
        selection = tasks.ConfirmedConversation("目标一", "确认好友", item)

        with patch.object(
            tasks,
            "scroll_and_select_user",
            return_value=[selection],
        ), patch.object(tasks, "_build_message") as build_message:
            with self.assertRaises(tasks.EditorSafetyError):
                tasks.do_user_task(
                    browser,
                    "草稿账号",
                    [],
                    ["目标一"],
                    runtime_config={**TEST_CONFIG, "browserTimeout": 1},
                )

        build_message.assert_not_called()
        self.assertEqual(chat_input.actions, [])
        self.assertTrue(context.closed)

    def test_placeholder_and_zero_width_leaf_do_not_become_false_draft(self):
        """Slate 占位文案及 BOM 零宽叶节点不应让真正空编辑器误报旧草稿。"""

        chat_input = FakeChatInput(
            initial_text="\ufeff",
            placeholder_text="发送消息",
        )
        # fake 显式复现 reviewer 指出的真实嵌套：leaf 文本包含 placeholder，但
        # data-slate-string 仍为空，因此安全读取结果应判定为无用户草稿。
        self.assertEqual(
            chat_input.locator(tasks.SLATE_TEXT_LEAF_SELECTOR).all_inner_texts(),
            ["发送消息\ufeff"],
        )

        selected_editor = tasks._get_unique_empty_editor(chat_input_page := FakePage(
            chat_input=chat_input
        ), 1)

        self.assertIs(selected_editor, chat_input)
        self.assertIn((tasks.CHAT_EDITOR_SELECTOR, 1), chat_input_page.waited_selectors)

    def test_enter_is_not_retried_when_editor_does_not_clear(self):
        """Enter 后文本未清空属于失败；即使等待超时也只能按一次 Enter。"""

        chat_input = FakeChatInput(clear_after_enter=False)
        page = FakePage(chat_input=chat_input, right_title="确认好友")
        context = FakeContext(page)
        browser = FakeBrowser(context)
        item = FakeConversationItem("确认好友")
        item.active = True
        selection = tasks.ConfirmedConversation("目标一", "确认好友", item)

        with patch.object(
            tasks,
            "scroll_and_select_user",
            return_value=[selection],
        ), patch.object(tasks, "_build_message", return_value="待提交消息"):
            with self.assertRaises(tasks.SubmissionConfirmationError):
                tasks.do_user_task(
                    browser,
                    "未清空账号",
                    [],
                    ["目标一"],
                    runtime_config={**TEST_CONFIG, "browserTimeout": 1},
                )

        self.assertEqual(chat_input.actions.count(("press", "Enter")), 1)
        self.assertTrue(context.closed)

    def test_multiple_editable_nodes_abort_before_message_build(self):
        """页面并存多个可编辑节点时不允许猜测首个节点并输入。"""

        chat_input = FakeChatInput(element_count=2)
        page = FakePage(chat_input=chat_input, right_title="确认好友")
        context = FakeContext(page)
        browser = FakeBrowser(context)
        item = FakeConversationItem("确认好友")
        item.active = True
        selection = tasks.ConfirmedConversation("目标一", "确认好友", item)

        with patch.object(
            tasks,
            "scroll_and_select_user",
            return_value=[selection],
        ), patch.object(tasks, "_build_message") as build_message:
            with self.assertRaises(tasks.EditorSafetyError):
                tasks.do_user_task(
                    browser,
                    "多编辑器账号",
                    [],
                    ["目标一"],
                    runtime_config={**TEST_CONFIG, "browserTimeout": 1},
                )

        build_message.assert_not_called()
        self.assertEqual(chat_input.actions, [])

    def test_context_is_closed_when_navigation_fails(self):
        page = FakePage(goto_error=RuntimeError("导航失败"))
        context = FakeContext(page)
        browser = FakeBrowser(context)

        with self.assertRaisesRegex(RuntimeError, "导航失败"):
            tasks.do_user_task(
                browser,
                "故障账号",
                [],
                ["目标"],
                runtime_config=TEST_CONFIG,
            )

        self.assertTrue(context.closed)

    def test_context_close_error_does_not_hide_original_task_error(self):
        page = FakePage(goto_error=RuntimeError("原始导航异常"))
        context = FakeContext(page, close_error=RuntimeError("次要关闭异常"))
        browser = FakeBrowser(context)

        with self.assertRaisesRegex(RuntimeError, "原始导航异常"):
            tasks.do_user_task(
                browser,
                "双重故障账号",
                [],
                ["目标"],
                runtime_config=TEST_CONFIG,
            )

        self.assertTrue(context.closed)

    def test_batch_isolates_account_failure_then_reports_overall_failure(self):
        users = [
            {"username": "账号一", "cookies": [], "targets": ["甲"]},
            {"username": "账号二", "cookies": [], "targets": ["乙"]},
        ]
        browser = FakeBrowser()
        playwright = FakePlaywright()
        processed = []

        def fake_do_user_task(
            ignored_browser, username, ignored_cookies, targets, runtime_config
        ):
            processed.append(username)
            if username == "账号一":
                raise RuntimeError("账号一页面异常")
            return tasks.TaskResult(
                username=username,
                state=tasks.TaskState.SUBMITTED_UNCONFIRMED,
                requested_targets=tuple(targets),
                submitted_targets=tuple(targets),
            )

        with patch.object(tasks, "do_user_task", side_effect=fake_do_user_task):
            with self.assertRaises(tasks.TaskBatchError) as raised:
                tasks.runTasks(
                    users=users,
                    runtime_config=TEST_CONFIG,
                    browser_factory=lambda runtime_config: (playwright, browser),
                )

        self.assertEqual(processed, ["账号一", "账号二"])
        self.assertTrue(browser.closed)
        self.assertTrue(playwright.stopped)
        self.assertEqual(
            [result.state for result in raised.exception.results],
            [tasks.TaskState.FAILED, tasks.TaskState.SUBMITTED_UNCONFIRMED],
        )

    def test_playwright_stops_even_when_browser_close_fails(self):
        browser = FakeBrowser(close_error=RuntimeError("浏览器关闭失败"))
        playwright = FakePlaywright()

        with self.assertRaises(tasks.TaskBatchError):
            tasks.runTasks(
                users=[],
                runtime_config=TEST_CONFIG,
                browser_factory=lambda runtime_config: (playwright, browser),
            )

        self.assertTrue(playwright.stopped)

    def test_importing_main_does_not_run_tasks(self):
        import main as app_main

        with patch.object(tasks, "runTasks") as run_tasks:
            importlib.reload(app_main)

        run_tasks.assert_not_called()

    def test_main_returns_nonzero_for_batch_failure(self):
        import main as app_main

        failed_result = tasks.TaskResult.failed(
            "故障账号", ["目标"], RuntimeError("页面异常")
        )
        with patch.object(app_main, "_load_environment_file"), patch.object(
            tasks,
            "runTasks",
            side_effect=tasks.TaskBatchError([failed_result]),
        ):
            exit_code = app_main.main()

        self.assertEqual(exit_code, 1)


if __name__ == "__main__":
    unittest.main()
