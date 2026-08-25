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


def make_authority_raw(
    size,
    *,
    has_more=False,
    sdk_is_loading=False,
    store_is_loading=False,
    ordered_ids=None,
    participant_sec_user_ids=None,
):
    """构造不含真实账号数据的权威状态原始值。"""

    ids = ordered_ids if ordered_ids is not None else [
        f"合成会话ID-{index}" for index in range(size)
    ]
    participant_ids = participant_sec_user_ids if participant_sec_user_ids is not None else [
        f"合成参与者sec_uid-{index}" for index in range(size)
    ]
    return {
        "hasMore": has_more,
        "sdkIsLoading": sdk_is_loading,
        "storeIsLoading": store_is_loading,
        "orderedIds": list(ids),
        "participantSecUserIds": list(participant_ids),
    }


def make_authority_snapshot(size, **kwargs):
    """通过生产校验后的字段形状构造冻结快照，供 proof 传播测试使用。"""

    raw = make_authority_raw(size, **kwargs)
    return tasks.ConversationAuthoritySnapshot(
        has_more=raw["hasMore"],
        sdk_is_loading=raw["sdkIsLoading"],
        store_is_loading=raw["storeIsLoading"],
        ordered_ids=tuple(raw["orderedIds"]),
        participant_sec_user_ids=tuple(raw["participantSecUserIds"]),
    )


def make_atomic_dom_snapshot(elements):
    """模拟浏览器在一个同步 JavaScript 任务内返回的会话 DOM 快照。

    Fake 直接从同一份元素列表读取全部字段，刻意不调用项目 Locator 的
    ``count/get_attribute/inner_text``。这样测试一旦误退回旧的分步读取路径就会
    由专门的竞态替身失败，而不是被静态 Fake 的即时返回掩盖生产虚拟列表问题。
    """

    return {
        "listContainerCount": 1,
        "items": [
            {
                "connected": item.connected,
                "stableIndex": (
                    None if item.stable_index is None else str(item.stable_index)
                ),
                "titleCount": 1,
                "displayName": item.display_name,
                "actionable": item.connected and item.actionable,
            }
            for item in elements
        ],
    }


def find_fake_item_by_stable_selector(selector, elements):
    """解析生产稳定索引选择器并返回唯一 Fake 会话项。

    生产代码不再保存 ``locator.all()`` 展开的易失 ``nth`` Locator，而是在原子
    快照后按 ``data-index`` 重建当前节点。Fake 使用生产选择器生成函数逐项比较，
    可以锁定这一契约；零项表示该 selector 不是索引定位，重复项则模拟 Playwright
    严格模式拒绝歧义，绝不能静默取第一个。
    """

    matches = [
        item
        for item in elements
        if type(item.stable_index) is int
        and selector
        == tasks._conversation_item_selector_for_stable_index(item.stable_index)
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise AssertionError("稳定索引选择器匹配到多个 Fake 会话项")
    return matches[0]


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


class IdentityIndexWhoseAliasGroupChanges(dict):
    """第二次读取时替换唯一身份，模拟计划后别名覆盖集合发生变化。"""

    def __init__(self, display_name, initial_identity, changed_identity):
        super().__init__({display_name: {initial_identity}})
        self.display_name = display_name
        self.changed_identity = changed_identity
        self.read_count = 0

    def get(self, key, default=None):
        if key == self.display_name:
            self.read_count += 1
            if self.read_count == 2:
                # 第一次读取建立两别名计划，第二次发生在点击前。这里仍保持身份
                # 集合大小为 1，专门验证生产代码会比较完整身份与别名组快照，而
                # 不是只看兼容字段 target_symbol 是否仍能命中。
                self[key] = {self.changed_identity}
        return super().get(key, default)


class IdentityIndexWhoseOtherConversationStartsMatching(dict):
    """第二次读取未选中显示名时，让它开始命中已选会话的同一别名。"""

    def __init__(self, other_display_name, late_identity, initial):
        super().__init__(initial)
        self.other_display_name = other_display_name
        self.late_identity = late_identity
        self.other_read_count = 0

    def get(self, key, default=None):
        if key == self.other_display_name:
            self.other_read_count += 1
            if self.other_read_count == 2:
                # 首次读取发生在原计划构建，第二次读取只能来自点击前完整重建。
                # 被选中项自己的匹配完全不变，只有全计划重建才能看到同一别名
                # 此刻落入两个会话，并在任何 click 之前整体取消。
                self[key] = {self.late_identity}
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
        editor_structure="standard",
    ):
        self.actions = []
        self.text = initial_text
        self.clear_after_enter = clear_after_enter
        self.element_count = element_count
        self.placeholder_text = placeholder_text
        # standard 模拟既有 Slate，custom 模拟生产 data-node/data-string 空态，
        # markerless/void 则用于证明未知或非文本内容始终失败关闭。
        self.editor_structure = editor_structure
        self.owner_page = None
        self.before_enter_hook = None
        self.last_guard_script = None
        self.last_content_state_script = None

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

    def evaluate(self, expression, expected=None):
        """模拟原子草稿分类或在同一编辑器上安装 Enter 守卫。"""

        if "readEditorContentState" in expression:
            self.last_content_state_script = expression
            if self.editor_structure in {"markerless", "detached"}:
                return tasks.EDITOR_CONTENT_UNKNOWN
            if self.editor_structure == "void":
                return tasks.EDITOR_CONTENT_PRESENT
            if self.editor_structure == "standard_multiple_empty_blocks":
                return tasks.EDITOR_CONTENT_PRESENT
            if self.editor_structure == "custom":
                return (
                    tasks.EDITOR_CONTENT_EMPTY
                    if self.text == "\u200b"
                    else tasks.EDITOR_CONTENT_PRESENT
                )
            # 标准空态允许测试用的 zero-width 字符；纯空格属于真实用户输入，
            # 不能再被 norm/trim 静默当成空编辑器。
            return (
                tasks.EDITOR_CONTENT_EMPTY
                if self.text in {"", "\ufeff", "\u200b"}
                else tasks.EDITOR_CONTENT_PRESENT
            )

        if "installEnterAuthorityGuard" not in expression:
            raise AssertionError("未预期的编辑器 evaluate 脚本")
        self.last_guard_script = expression
        if self.owner_page is None:
            raise AssertionError("测试编辑器尚未绑定页面，无法安装 Enter 守卫")
        return self.owner_page.install_fake_enter_guard(self, expected)

    def press(self, value):
        self.actions.append(("press", value))
        if value == "Shift+Enter":
            self.text += "\n"
        elif value == "Enter":
            # hook 精确位于 Python 最后 proof 已返回、真实 keydown capture 即将运行
            # 的窗口，用来复现仅靠 press 前检查无法封住的 authority 竞态。
            if self.before_enter_hook is not None:
                hook = self.before_enter_hook
                self.before_enter_hook = None
                hook()
            keydown_allowed = (
                self.owner_page is None
                or self.owner_page.dispatch_fake_enter_phase("keydown", self)
            )
            # 站点若使用 keydown 发送，会在同一事件传播内先于后续相位处理；只有
            # 最早 capture 已明确 allowed 时，fake 才模拟这一清空副作用。
            if keydown_allowed and self.clear_after_enter:
                # 生产自定义编辑器发送后恢复精确 U+200B 空标记；标准 Slate fake
                # 则恢复 FEFF zero-width。不能用空字符串替代真实清空结构。
                self.text = (
                    "\u200b"
                    if self.editor_structure == "custom"
                    else "\ufeff"
                )
            if self.owner_page is not None:
                # 真实 Chromium 即使 keydown 被阻断仍会产生 keyup。fake 显式派发
                # 全部潜在 Enter 相位，验证预装门禁不会让站点改用 keyup 绕过。
                for phase in ("keypress", "beforeinput", "keyup"):
                    self.owner_page.dispatch_fake_enter_phase(phase, self)


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
        self.click_count = 0
        self.click_timeout = None
        self.active = False
        self.authority_page = None
        self.connected = True
        self.actionable = True
        self.last_evaluate_expression = None

    def locator(self, selector):
        if selector == tasks.CONVERSATION_TITLE_SELECTOR:
            return FakeTextLocator(self.display_name)
        if selector == tasks.CONVERSATION_INDEX_ANCESTOR_SELECTOR:
            return self.IndexedAncestor(self.stable_index)
        raise AssertionError(f"未预期的会话项选择器：{selector}")

    def click(self, timeout=None):
        """模拟独立的 Playwright 可信点击，并记录生产限定的点击超时。"""

        self.click_timeout = timeout
        self.clicked = True
        self.click_count += 1
        if self.activate_on_click:
            self.active = True

    def evaluate(self, expression, expected=None):
        """模拟只读几何探针与生产原子点击边界。"""

        if "conversationItemIsActionableInList" in expression:
            # 搜索阶段只能把已经进入列表容器可交互区域的 DOM 项设为候选。
            # ``actionable`` 可由测试在不同虚拟窗口分别设置，以复现 overscan。
            return self.connected and self.actionable

        if "clickAtAuthoritativeConversationBoundary" not in expression:
            raise AssertionError("未预期的会话项 evaluate 脚本")
        self.last_evaluate_expression = expression
        if self.authority_page is None:
            return "AUTHORITY_BOUNDARY_REJECTED"
        current = self.authority_page.current_authority_raw
        expected_raw = {
            "hasMore": expected["hasMore"],
            "sdkIsLoading": expected["sdkIsLoading"],
            "storeIsLoading": expected["storeIsLoading"],
            "orderedIds": expected["orderedIds"],
            "participantSecUserIds": expected["participantSecUserIds"],
        }
        if (
            not self.connected
            or not self.actionable
            or self.stable_index != expected["stableIndex"]
            or tasks._normalize_identity_value(self.display_name)
            != expected["displayName"]
            or current != expected_raw
        ):
            return "AUTHORITY_BOUNDARY_REJECTED"
        # evaluate 只能完成原子只读授权，不能替代 Playwright 的可信鼠标事件。若在
        # 这里直接激活会话，单测会再次掩盖浏览器拒绝 isTrusted=false 的线上回归。
        return "AUTHORITY_BOUNDARY_AUTHORIZED"

    def get_attribute(self, attribute_name, timeout=None):
        if attribute_name != "class":
            raise AssertionError(f"未预期读取属性：{attribute_name}")
        classes = ["conversationConversationItemwrapper"]
        if self.active:
            classes.append(tasks.CURRENT_CONVERSATION_CLASS)
        return " ".join(classes)


class PreClickRacyConversationItem(FakeConversationItem):
    """复现线上 ``count()==1`` 后属性读取超时的动态会话项。

    旧扫描会在第一次点击前分两次读取祖先，因而必然触发这里的合成 TimeoutError；
    新扫描必须完全绕开该路径。点击后的双重确认仍允许正常读取索引，确保测试验证
    的是“预扫描与发送搜索采用原子快照”，而不是粗暴删除全部后置身份核验。
    """

    class RacyIndexedAncestor:
        def __init__(self, owner):
            self.owner = owner

        def count(self):
            return 1

        def get_attribute(self, attribute_name, timeout=None):
            if attribute_name != "data-index":
                raise AssertionError(f"未预期读取竞态索引属性：{attribute_name}")
            if not self.owner.clicked:
                self.owner.pre_click_attribute_reads += 1
                raise TimeoutError(
                    "Locator.get_attribute: Timeout 100ms exceeded. 合成敏感内容"
                )
            return str(self.owner.stable_index)

    def __init__(self, display_name, stable_index=0):
        super().__init__(display_name, stable_index=stable_index)
        self.pre_click_attribute_reads = 0

    def locator(self, selector):
        if selector == tasks.CONVERSATION_INDEX_ANCESTOR_SELECTOR:
            return self.RacyIndexedAncestor(self)
        return super().locator(selector)


class FakePage:
    """仅实现 ``do_user_task`` 在受测路径会使用的同步页面接口。"""

    def __init__(
        self,
        chat_input=None,
        goto_error=None,
        right_title="确认好友",
        authority_raw=None,
    ):
        self.chat_input = chat_input or FakeChatInput()
        self.goto_error = goto_error
        self.right_title = FakeTextLocator(right_title)
        self.events = {}
        self.waited_selectors = []
        self.waited_milliseconds = []
        self.goto_urls = []
        self.goto_options = []
        self.authority_raw = authority_raw or make_authority_raw(1)
        self.evaluated_expressions = []
        self.enter_guard = None
        self.enter_guard_cleanup_count = 0
        self.enter_capture_gate_preinstalled = False
        self.enter_gate_status = tasks.ENTER_AUTHORITY_GUARD_DISARMED
        self.enter_phase_events = []
        self.blocked_enter_phases = []
        self.lifecycle_events = []
        self.chat_input.owner_page = self

    @property
    def current_authority_raw(self):
        return dict(self.authority_raw)

    def on(self, event_name, callback):
        self.events[event_name] = callback

    def goto(self, url, **options):
        self.lifecycle_events.append("goto")
        self.goto_urls.append(url)
        self.goto_options.append(options)
        if self.goto_error is not None:
            raise self.goto_error

    def wait_for_selector(self, selector, timeout):
        self.waited_selectors.append((selector, timeout))

    def wait_for_timeout(self, milliseconds):
        self.waited_milliseconds.append(milliseconds)

    def install_fake_enter_guard(self, editor, expected):
        """仅保存合成 proof；真正放行判断延迟到 fake keydown 时同步执行。"""

        if (
            not self.enter_capture_gate_preinstalled
            or self.enter_guard is not None
            or self.enter_gate_status != tasks.ENTER_AUTHORITY_GUARD_DISARMED
            or editor is not self.chat_input
        ):
            return "ENTER_AUTHORITY_GUARD_SETUP_ERROR"
        expected_authority = {
            "hasMore": expected["hasMore"],
            "sdkIsLoading": expected["sdkIsLoading"],
            "storeIsLoading": expected["storeIsLoading"],
            "orderedIds": expected["orderedIds"],
            "participantSecUserIds": expected["participantSecUserIds"],
        }
        if self.current_authority_raw != expected_authority:
            return "ENTER_AUTHORITY_GUARD_SETUP_ERROR"
        self.enter_guard = {
            "editor": editor,
            "expected": expected,
        }
        self.enter_gate_status = tasks.ENTER_AUTHORITY_GUARD_ARMED
        return tasks.ENTER_AUTHORITY_GUARD_ARMED

    def dispatch_fake_enter_phase(self, phase, editor):
        """模拟最早 capture 对 keydown 及 Chromium 后续 Enter 相位的处理。"""

        self.enter_phase_events.append(phase)
        if not self.enter_capture_gate_preinstalled:
            return True
        if phase != "keydown":
            # 经过验证的 keydown 是唯一允许传播的发送相位；keypress、beforeinput
            # 与 keyup 全部阻断，避免站点通过另一相位重复或绕过提交。
            self.blocked_enter_phases.append(phase)
            return False
        if (
            self.enter_guard is None
            or self.enter_gate_status != tasks.ENTER_AUTHORITY_GUARD_ARMED
        ):
            self.enter_gate_status = tasks.ENTER_AUTHORITY_GUARD_BLOCKED
            self.blocked_enter_phases.append(phase)
            return False
        expected = self.enter_guard["expected"]
        expected_authority = {
            "hasMore": expected["hasMore"],
            "sdkIsLoading": expected["sdkIsLoading"],
            "storeIsLoading": expected["storeIsLoading"],
            "orderedIds": expected["orderedIds"],
            "participantSecUserIds": expected["participantSecUserIds"],
        }
        allowed = (
            editor is self.enter_guard["editor"]
            and editor is self.chat_input
            and editor.count() == 1
            and self.right_title.count() == 1
            and tasks._normalize_identity_value(self.right_title.text)
            == expected["displayName"]
            and self.current_authority_raw == expected_authority
        )
        self.enter_gate_status = (
            tasks.ENTER_AUTHORITY_GUARD_ALLOWED
            if allowed
            else tasks.ENTER_AUTHORITY_GUARD_BLOCKED
        )
        if not allowed:
            self.blocked_enter_phases.append(phase)
        return allowed

    def dispatch_fake_enter_keydown(self, editor):
        """保留旧测试入口，实际统一走完整 Enter 相位模型。"""

        return self.dispatch_fake_enter_phase("keydown", editor)

    def consume_fake_enter_guard_status(self):
        """模拟 finally cleanup：状态只读一次，随后清除页面私有守卫。"""

        if not self.enter_capture_gate_preinstalled:
            return "ENTER_AUTHORITY_GUARD_STATUS_MISSING"
        status = self.enter_gate_status
        self.enter_guard = None
        self.enter_gate_status = tasks.ENTER_AUTHORITY_GUARD_DISARMED
        self.enter_guard_cleanup_count += 1
        return status

    def evaluate(self, expression, ignored_handle=None):
        self.evaluated_expressions.append(expression)
        if "consumeEnterAuthorityGuardStatus" in expression:
            return self.consume_fake_enter_guard_status()
        if "readAuthoritativeConversationSnapshot" in expression:
            return self.current_authority_raw
        raise AssertionError(f"测试未预期访问页面脚本：{expression}")

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
        self.init_scripts = []
        self.lifecycle_events = self.page.lifecycle_events

    def set_default_navigation_timeout(self, value):
        self.navigation_timeout = value

    def set_default_timeout(self, value):
        self.default_timeout = value

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def add_init_script(self, script=None, path=None):
        if path is not None or not isinstance(script, str):
            raise AssertionError("测试只接受内联 context init script")
        self.lifecycle_events.append("add_init_script")
        self.init_scripts.append(script)

    def new_page(self):
        self.lifecycle_events.append("new_page")
        self.page.enter_capture_gate_preinstalled = bool(self.init_scripts)
        self.page.enter_gate_status = tasks.ENTER_AUTHORITY_GUARD_DISARMED
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

        def evaluate_all(self, expression, selectors):
            # 锁定生产代码使用单次浏览器快照；旧 ``all()`` 方法被有意移除，
            # 任何回退到动态 nth Locator 的实现都会立刻以 AttributeError 失败。
            if "readAtomicConversationDomSnapshot" not in expression:
                raise AssertionError("未预期的会话 DOM 快照脚本")
            if selectors != {
                "list": tasks.CONVERSATION_LIST_SELECTOR,
                "title": tasks.CONVERSATION_TITLE_SELECTOR,
            }:
                raise AssertionError("原子会话快照选择器参数不完整")
            return make_atomic_dom_snapshot(self.elements)

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
        self.authority_raw = make_authority_raw(1)
        self.element.authority_page = self

    @property
    def current_authority_raw(self):
        return dict(self.authority_raw)

    def locator(self, selector):
        if selector == tasks.CONVERSATION_ITEM_SELECTOR:
            return self.ItemsLocator([self.element])
        indexed_item = find_fake_item_by_stable_selector(selector, [self.element])
        if indexed_item is not None:
            indexed_item.authority_page = self
            return indexed_item
        if selector == tasks.CONVERSATION_LIST_SELECTOR:
            return self.ScrollLocator(self.scroll_handle)
        if selector == tasks.RIGHT_PANEL_TITLE_SELECTOR:
            return FakeTextLocator("延迟好友")
        raise AssertionError(f"未预期的列表选择器：{selector}")

    def evaluate(self, expression, ignored_handle=None):
        # 单页列表从一开始就位于底部；同时支持生产代码的回顶表达式。
        if "readAuthoritativeConversationSnapshot" in expression:
            return self.current_authority_raw
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
                sec_uid="合成参与者sec_uid-0",
                nickname="延迟好友",
                remark_name="延迟好友",
            )
        }


class FakeConversationListPage:
    """模拟按视口虚拟化的列表，支持预扫描、到底证据和回顶验证。"""

    class ItemsLocator:
        def __init__(self, elements):
            self.elements = elements

        def evaluate_all(self, expression, selectors):
            # Fake 返回与真实 evaluate_all 相同的序列化结构，不允许生产代码逐项
            # 调用 Locator；这同时覆盖预扫描和发送阶段两条历史竞态路径。
            if "readAtomicConversationDomSnapshot" not in expression:
                raise AssertionError("未预期的会话 DOM 快照脚本")
            if selectors != {
                "list": tasks.CONVERSATION_LIST_SELECTOR,
                "title": tasks.CONVERSATION_TITLE_SELECTOR,
            }:
                raise AssertionError("原子会话快照选择器参数不完整")
            return make_atomic_dom_snapshot(self.elements)

    class ScrollLocator:
        def __init__(self, handle):
            self.handle = handle

        def element_handle(self):
            return self.handle

        def count(self):
            return 1

    def __init__(self, elements, right_title, *, pages=None, authority_raw=None):
        # 单页调用保持简洁；多页用 pages 显式模拟后续虚拟页，确保首屏不会看到
        # 后页元素，正好覆盖“先发送、后发现同名”的历史风险。
        self.pages = pages if pages is not None else [elements]
        self.right_title = FakeTextLocator(right_title)
        self.scroll_handle = object()
        self.scroll_top = 0
        self.client_height = 100
        self.waited_milliseconds = []
        stable_indices = [
            item.stable_index
            for page_items in self.pages
            for item in page_items
            if item.stable_index is not None
        ]
        authority_size = max(stable_indices) + 1 if stable_indices else 1
        self.authority_raw = authority_raw or make_authority_raw(authority_size)

    @property
    def current_authority_raw(self):
        return dict(self.authority_raw)

    @property
    def visible_elements(self):
        page_index = min(
            int(self.scroll_top // self.client_height),
            len(self.pages) - 1,
        )
        return self.pages[page_index]

    def locator(self, selector):
        if selector == tasks.CONVERSATION_ITEM_SELECTOR:
            for item in self.visible_elements:
                # 多目标重排测试会在生成器暂停后替换 pages；每次取 locator 时重新
                # 绑定 owner，保证新建的 fake 项也能执行原子 authority 点击门禁。
                item.authority_page = self
            return self.ItemsLocator(self.visible_elements)
        indexed_item = find_fake_item_by_stable_selector(
            selector,
            self.visible_elements,
        )
        if indexed_item is not None:
            indexed_item.authority_page = self
            return indexed_item
        if selector == tasks.CONVERSATION_LIST_SELECTOR:
            return self.ScrollLocator(self.scroll_handle)
        if selector == tasks.RIGHT_PANEL_TITLE_SELECTOR:
            return self.right_title
        raise AssertionError(f"未预期的列表选择器：{selector}")

    def evaluate(self, expression, ignored_handle=None):
        scroll_height = self.client_height * len(self.pages)
        if "readAuthoritativeConversationSnapshot" in expression:
            return self.current_authority_raw
        if "scrollTop:" in expression:
            return {
                "scrollTop": self.scroll_top,
                "clientHeight": self.client_height,
                "scrollHeight": scroll_height,
            }
        if "scrollTop = 0" in expression:
            self.scroll_top = 0
            return self.scroll_top
        if "requestAnimationFrame" in expression:
            # 生产代码在底部做一次轻微上移再回底的无副作用触碰。普通 fake 不
            # 模拟动画帧，只需保留“最终回到当前底部”这一安全后置条件。
            self.scroll_top = scroll_height - self.client_height
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


class RerenderingConversationListPage(FakeConversationListPage):
    """按脚本返回异常、空窗或成功快照，模拟虚拟列表整棵重绘。"""

    class RerenderingItemsLocator:
        def __init__(self, owner):
            self.owner = owner

        def evaluate_all(self, expression, selectors):
            if "readAtomicConversationDomSnapshot" not in expression:
                raise AssertionError("未预期的重绘会话 DOM 快照脚本")
            if selectors != {
                "list": tasks.CONVERSATION_LIST_SELECTOR,
                "title": tasks.CONVERSATION_TITLE_SELECTOR,
            }:
                raise AssertionError("重绘快照选择器参数不完整")
            self.owner.atomic_snapshot_calls += 1
            outcome_index = min(
                self.owner.atomic_snapshot_calls - 1,
                len(self.owner.snapshot_outcomes) - 1,
            )
            outcome = self.owner.snapshot_outcomes[outcome_index]
            if outcome == "error":
                raise RuntimeError("合成瞬时 DOM 异常，含不应泄漏的页面内容")
            if outcome == "empty":
                return make_atomic_dom_snapshot([])
            if outcome == "ok":
                return make_atomic_dom_snapshot(self.owner.visible_elements)
            raise AssertionError(f"未预期的原子快照结果脚本：{outcome}")

    def __init__(self, elements, right_title, snapshot_outcomes):
        if not snapshot_outcomes:
            raise ValueError("重绘快照脚本不能为空")
        super().__init__(elements, right_title)
        self.snapshot_outcomes = list(snapshot_outcomes)
        self.atomic_snapshot_calls = 0

    def locator(self, selector):
        if selector == tasks.CONVERSATION_ITEM_SELECTOR:
            for item in self.visible_elements:
                item.authority_page = self
            return self.RerenderingItemsLocator(self)
        return super().locator(selector)


class ScriptedInventoryPage:
    """按独立 pass 提供库存快照，用于复现线上分批懒加载。

    页面始终建模为单视口底部，因此测试聚焦于两层稳定性协议，而不重复验证滚动
    容器几何。每次生产代码执行“验证回顶”就进入下一个脚本 pass；最终成功后的
    回顶会被钳制到最后一份脚本，不改变已经返回的库存。所有项目都是真实的
    ``FakeConversationItem``，便于统一断言验收完成或失败前 click_count 始终为零。
    """

    class ItemsLocator:
        def __init__(self, owner):
            self.owner = owner

        def evaluate_all(self, expression, selectors):
            # 脚本型库存每个 pass 都在调用瞬间取 current_items，模拟虚拟列表已经
            # 切换到新窗口；整个窗口仍由一次 evaluate_all 原子序列化。
            if "readAtomicConversationDomSnapshot" not in expression:
                raise AssertionError("未预期的会话 DOM 快照脚本")
            if selectors != {
                "list": tasks.CONVERSATION_LIST_SELECTOR,
                "title": tasks.CONVERSATION_TITLE_SELECTOR,
            }:
                raise AssertionError("原子会话快照选择器参数不完整")
            return make_atomic_dom_snapshot(self.owner.current_items)

    class ScrollLocator:
        def __init__(self, handle):
            self.handle = handle

        def element_handle(self):
            return self.handle

        def count(self):
            return 1

    def __init__(
        self,
        pass_sizes,
        *,
        append_after_bottom_wait=None,
        authority_states=None,
        authority_error=None,
    ):
        if not pass_sizes:
            raise ValueError("测试脚本至少需要一个 pass")
        self.pass_items = [
            [
                FakeConversationItem(
                    f"脚本好友-{stable_index}",
                    stable_index=stable_index,
                )
                for stable_index in range(size)
            ]
            for size in pass_sizes
        ]
        self.all_items = [
            item for items in self.pass_items for item in items
        ]
        self.scroll_handle = object()
        self.pass_index = -1
        self.awaiting_reset_wait = False
        self.bottom_wait_count = 0
        self.append_after_bottom_wait = append_after_bottom_wait
        self.appended_item = None
        self.authority_states = authority_states
        self.authority_error = authority_error
        self.authority_read_count = 0

    @property
    def current_items(self):
        return self.pass_items[max(self.pass_index, 0)]

    @property
    def current_authority_raw(self):
        if self.authority_error is not None:
            raise self.authority_error
        if self.authority_states is None:
            return make_authority_raw(len(self.current_items))
        state = self.authority_states[
            min(max(self.pass_index, 0), len(self.authority_states) - 1)
        ]
        return dict(state)

    def locator(self, selector):
        if selector == tasks.CONVERSATION_ITEM_SELECTOR:
            for item in self.current_items:
                item.authority_page = self
            return self.ItemsLocator(self)
        if selector == tasks.CONVERSATION_LIST_SELECTOR:
            return self.ScrollLocator(self.scroll_handle)
        raise AssertionError(f"未预期的脚本库存选择器：{selector}")

    def evaluate(self, expression, ignored_handle=None):
        if "readAuthoritativeConversationSnapshot" in expression:
            self.authority_read_count += 1
            return self.current_authority_raw
        if "scrollTop:" in expression:
            return {"scrollTop": 0, "clientHeight": 100, "scrollHeight": 100}
        if "scrollTop = 0" in expression:
            # 每个独立 pass 都必须从受验证的回顶动作开始。最后一次成功回顶也会
            # 进入这里，因此索引钳制到末尾，避免读取脚本范围之外的数据。
            self.pass_index = min(self.pass_index + 1, len(self.pass_items) - 1)
            self.awaiting_reset_wait = True
            self.bottom_wait_count = 0
            return 0
        raise AssertionError(f"未预期的脚本库存脚本：{expression}")

    def wait_for_timeout(self, ignored_milliseconds):
        if self.awaiting_reset_wait:
            # _reset_list_to_top 自带一次布局等待；它不是底部稳定快照之间的等待，
            # 不应提前触发专门为第二个底部等待安排的慢追加。
            self.awaiting_reset_wait = False
            return
        self.bottom_wait_count += 1
        if (
            self.append_after_bottom_wait is not None
            and self.appended_item is None
            and self.bottom_wait_count == self.append_after_bottom_wait
        ):
            # 在第二个底部等待后追加，能证明“三快照”要求不会像旧两快照逻辑那样
            # 在追加发生前返回。新增项会永久保留到后续独立 pass。
            stable_index = len(self.current_items)
            self.appended_item = FakeConversationItem(
                f"脚本好友-{stable_index}",
                stable_index=stable_index,
            )
            for pass_items in self.pass_items[self.pass_index :]:
                pass_items.append(self.appended_item)
            self.all_items.append(self.appended_item)


class DelayedTerminalInventoryPage(ScriptedInventoryPage):
    """先连续观察两次 45 项非终态，再切换到 150 项权威终态。"""

    def __init__(self):
        super().__init__([45, 150, 150])
        self.terminal_reached = False
        self.bottom_observations = []

    @property
    def current_authority_raw(self):
        size = 150 if self.terminal_reached else 45
        return make_authority_raw(size, has_more=not self.terminal_reached)

    def wait_for_timeout(self, ignored_milliseconds):
        if self.awaiting_reset_wait:
            super().wait_for_timeout(ignored_milliseconds)
            return
        self.bottom_observations.append(
            (len(self.current_authority_raw["orderedIds"]), self.current_authority_raw["hasMore"])
        )
        if len(self.bottom_observations) == 2:
            # 两次完全相同的 45 项 DOM/authority 观察仍是 hasMore=true，不能早退。
            # 切换 authority 后生产代码必须丢弃旧库存、回顶，再读取 150 项 DOM。
            self.terminal_reached = True
        super().wait_for_timeout(ignored_milliseconds)


class MidScanAuthorityChangePage(ScriptedInventoryPage):
    """在第一个可见窗口读完后切换 authority，验证旧库存不会混入新 pass。"""

    def __init__(self):
        super().__init__([1, 1, 1])
        self.pass_items[0][0].display_name = "旧顺序标题"
        self.pass_items[1][0].display_name = "新顺序标题"
        self.pass_items[2][0].display_name = "新顺序标题"
        self.authority_read_count = 0
        self.switched = False

    @property
    def current_authority_raw(self):
        ordered_ids = ["新权威ID"] if self.switched else ["旧权威ID"]
        return make_authority_raw(1, ordered_ids=ordered_ids)

    def evaluate(self, expression, ignored_handle=None):
        if "readAuthoritativeConversationSnapshot" in expression:
            self.authority_read_count += 1
            # 调用顺序为：初始 proof、窗口前 proof、窗口后 proof。第三次读取才切换，
            # 正好验证临时窗口已经读取但尚未合并时的竞态处理。
            if self.authority_read_count == 3:
                self.switched = True
            return self.current_authority_raw
        return super().evaluate(expression, ignored_handle)


class TaskReliabilityTests(unittest.TestCase):
    def test_atomic_snapshot_bypasses_pre_click_attribute_timeout_race(self):
        """扫描不得再触发 count 成功后 get_attribute 超时的旧竞态路径。"""

        item = PreClickRacyConversationItem("重绘好友", stable_index=0)
        page = FakeConversationListPage([item], right_title="重绘好友")

        selected = list(
            tasks.scroll_and_select_user(
                page,
                "原子快照账号",
                ["重绘好友"],
                identity_index=None,
                friend_list_wait_time=1,
                confirmation_timeout=100,
            )
        )

        # 旧实现会在首次预扫描就触发合成 TimeoutError；新实现应一直到原子点击后
        # 才由确认探针读取祖先。零次点击前属性读取证明预扫描和发送搜索均已迁移。
        self.assertEqual(item.pre_click_attribute_reads, 0)
        self.assertEqual(item.click_count, 1)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].display_name, "重绘好友")

    def test_atomic_snapshot_retries_error_and_empty_rerender_then_recovers(self):
        """协议异常与整棵空窗各一次后，应在第三个原子观察点安全恢复。"""

        item = FakeConversationItem("恢复好友", stable_index=0)
        page = RerenderingConversationListPage(
            [item],
            right_title="恢复好友",
            snapshot_outcomes=["error", "empty", "ok"],
        )
        inventory = {}

        added_count = tasks._record_visible_inventory(page, inventory)

        self.assertEqual(inventory, {0: "恢复好友"})
        self.assertEqual(added_count, 1)
        self.assertEqual(page.atomic_snapshot_calls, 3)
        self.assertEqual(
            page.waited_milliseconds,
            [
                tasks.DOM_CONFIRM_POLL_INTERVAL_MS,
                tasks.DOM_CONFIRM_POLL_INTERVAL_MS,
            ],
        )
        self.assertEqual(item.click_count, 0)

    def test_atomic_snapshot_permanent_failure_is_bounded_and_redacted(self):
        """持续重绘异常只能有限重试，且错误正文和页面内容不得进入结果。"""

        item = FakeConversationItem("不会泄漏的好友标题", stable_index=0)
        page = RerenderingConversationListPage(
            [item],
            right_title="不会泄漏的好友标题",
            snapshot_outcomes=["error"],
        )
        inventory = {}

        with self.assertRaises(tasks.ConversationSelectionError) as caught:
            tasks._record_visible_inventory(page, inventory)

        self.assertEqual(
            page.atomic_snapshot_calls,
            tasks.MAX_ATOMIC_DOM_SNAPSHOT_ATTEMPTS,
        )
        self.assertEqual(
            len(page.waited_milliseconds),
            tasks.MAX_ATOMIC_DOM_SNAPSHOT_ATTEMPTS - 1,
        )
        self.assertNotIn("合成瞬时 DOM 异常", str(caught.exception))
        self.assertNotIn("不会泄漏的好友标题", str(caught.exception))
        self.assertEqual(inventory, {})
        self.assertEqual(item.click_count, 0)

    def test_authority_participant_strong_id_selects_offscreen_same_name(self):
        """同名 DOM 不影响 sec_uid join；强标识只授权对应 participant 索引。"""

        selected_identity = tasks.FriendIdentity(
            "短号甲", "强抖音号甲", "sec甲", "同名好友", "同名好友"
        )
        other_identity = tasks.FriendIdentity(
            "短号乙", "强抖音号乙", "sec乙", "同名好友", "同名好友"
        )
        authority = make_authority_snapshot(
            2,
            participant_sec_user_ids=["sec甲", "sec乙"],
        )

        plan = tasks._build_unique_selection_plan(
            {0: "同名好友", 1: "同名好友"},
            ["强抖音号甲", "同名好友"],
            {"同名好友": {selected_identity, other_identity}},
            authority=authority,
        )

        self.assertEqual(set(plan), {0})
        self.assertEqual(
            plan[0].match.covered_targets,
            ("强抖音号甲", "同名好友"),
        )

    def test_authority_participant_rejects_nickname_only_group(self):
        """即使 participant 可 join，没有强标识锚点的纯昵称配置仍须失败。"""

        identity = tasks.FriendIdentity(
            "短号甲", "强抖音号甲", "sec甲", "昵称甲", "昵称甲"
        )
        with self.assertRaises(tasks.ConversationSelectionError):
            tasks._build_unique_selection_plan(
                {0: "昵称甲"},
                ["昵称甲"],
                {"昵称甲": {identity}},
                authority=make_authority_snapshot(
                    1,
                    participant_sec_user_ids=["sec甲"],
                ),
            )

    def test_participant_change_after_snapshot_blocks_atomic_click(self):
        """remote proof 后 participant 顺序变化时，原子边界必须保持零点击。"""

        proof = make_authority_snapshot(
            2,
            participant_sec_user_ids=["sec甲", "sec乙"],
        )
        item = FakeConversationItem("目标好友", stable_index=0)
        page = FakeConversationListPage(
            [item, FakeConversationItem("其他好友", stable_index=1)],
            right_title="目标好友",
            authority_raw=make_authority_raw(
                2,
                participant_sec_user_ids=["sec乙", "sec甲"],
            ),
        )
        item.authority_page = page

        with self.assertRaises(tasks.ConversationSelectionError):
            tasks._click_conversation_at_authority_boundary(
                item,
                0,
                "目标好友",
                proof,
            )

        self.assertEqual(item.click_count, 0)

    def test_inventory_waits_for_consecutive_full_passes_after_partial_first_pass(self):
        """45、45 两份非终态不能早退，150、150 终态才允许返回。"""

        page = DelayedTerminalInventoryPage()

        inventory, returned_handle, proof = tasks._scan_full_conversation_inventory(
            page,
            friend_list_wait_time=1,
        )

        self.assertEqual(len(inventory), 150)
        self.assertEqual(len(proof.ordered_ids), 150)
        self.assertIs(returned_handle, page.scroll_handle)
        self.assertEqual(page.pass_index, 2)
        self.assertEqual(page.bottom_observations[:2], [(45, True), (45, True)])
        self.assertIn((150, False), page.bottom_observations)
        self.assertTrue(all(item.click_count == 0 for item in page.all_items))

    def test_inventory_fails_closed_when_four_full_passes_never_match(self):
        """四个 pass 的完整映射始终变化时，达到上限也不能接受任一快照。"""

        page = ScriptedInventoryPage([1, 2, 3, 4])

        with self.assertRaises(tasks.ConversationSelectionError):
            tasks._scan_full_conversation_inventory(
                page,
                friend_list_wait_time=1,
            )

        self.assertEqual(page.pass_index, tasks.MAX_INVENTORY_SCAN_PASSES - 1)
        self.assertTrue(all(item.click_count == 0 for item in page.all_items))

    def test_stable_short_inventory_passes_two_independent_scans_without_click(self):
        """确实稳定的短列表仍可通过，但扫描层自身始终不得触发点击。"""

        page = ScriptedInventoryPage([3, 3])

        inventory, _, proof = tasks._scan_full_conversation_inventory(
            page,
            friend_list_wait_time=1,
        )

        self.assertEqual(len(inventory), 3)
        self.assertTrue(proof.is_terminal)
        self.assertTrue(all(item.click_count == 0 for item in page.all_items))

    def test_slow_bottom_append_before_third_snapshot_is_included(self):
        """第二个等待后才追加的项目必须打断稳定计数并进入最终库存。"""

        page = ScriptedInventoryPage(
            [2, 2],
            append_after_bottom_wait=2,
        )

        inventory, _, proof = tasks._scan_full_conversation_inventory(
            page,
            friend_list_wait_time=1,
        )

        self.assertIsNotNone(page.appended_item)
        self.assertEqual(len(inventory), 3)
        self.assertEqual(len(proof.ordered_ids), 3)
        self.assertIn(2, inventory)
        self.assertTrue(all(item.click_count == 0 for item in page.all_items))

    def test_authority_snapshot_hides_ids_from_repr(self):
        """冻结 proof 可比较完整顺序，但 repr 不得泄露服务端会话 ID。"""

        proof = make_authority_snapshot(
            1,
            ordered_ids=["不应出现在repr中的合成ID"],
        )

        self.assertNotIn("不应出现在repr中的合成ID", repr(proof))
        self.assertTrue(proof.is_terminal)

    def test_authority_scripts_follow_live_remote_factory_shape(self):
        """锁定线上已确认的 remote.get('.') -> factory() 解析顺序。"""

        page = FakePage()
        proof = tasks._read_authoritative_conversation_snapshot(page)
        reader_script = page.evaluated_expressions[-1]
        self.assertIn('await remote.get(".")', reader_script)
        self.assertIn("const exportsObject = factory()", reader_script)
        self.assertIn("exportsObject.Context", reader_script)
        self.assertIn('"initLinkInstance"', reader_script)
        self.assertIn("link.isLoading", reader_script)

        item = FakeConversationItem("工厂形状目标", stable_index=0)
        list_page = FakeConversationListPage(
            [item],
            right_title="工厂形状目标",
        )
        item.authority_page = list_page
        tasks._click_conversation_at_authority_boundary(
            item,
            0,
            "工厂形状目标",
            proof,
        )
        click_script = item.last_evaluate_expression
        self.assertIn('await remote.get(".")', click_script)
        self.assertLess(
            click_script.index('await remote.get(".")'),
            click_script.index("nextParams.hasMore"),
        )
        self.assertNotIn("await", click_script[click_script.index("nextParams.hasMore") :])
        self.assertIn("Number.isSafeInteger(expected.stableIndex)", click_script)
        self.assertIn("element.ownerDocument !== document", click_script)
        self.assertIn("element.matches(", click_script)
        # 页面脚本只能完成原子授权；真正激活会话必须来自随后独立执行、带短超时
        # 的 Playwright 可信点击，防止 fake 再次掩盖 isTrusted=false 的线上回归。
        self.assertNotIn("HTMLElement.prototype.click.call(element)", click_script)
        self.assertIn('return "AUTHORITY_BOUNDARY_AUTHORIZED"', click_script)
        self.assertEqual(item.click_count, 1)
        self.assertEqual(item.click_timeout, tasks.CONVERSATION_CLICK_TIMEOUT_MS)

    def test_authority_reader_rejects_wrong_types_duplicate_or_empty_ids(self):
        """权威字段必须严格为 bool 与非空、全局唯一的字符串有序列表。"""

        invalid_states = {
            "hasMore 不是 bool": {
                **make_authority_raw(1),
                "hasMore": 0,
            },
            "loading 不是 bool": {
                **make_authority_raw(1),
                "sdkIsLoading": "false",
            },
            "ID 重复": make_authority_raw(
                2,
                ordered_ids=["合成机密ID", "合成机密ID"],
            ),
            "ID 为空": make_authority_raw(1, ordered_ids=["   "]),
        }

        for label, raw_state in invalid_states.items():
            with self.subTest(label=label):
                page = FakePage(authority_raw=raw_state)
                with self.assertRaises(tasks.ConversationSelectionError) as caught:
                    tasks._read_authoritative_conversation_snapshot(page)
                self.assertNotIn("合成机密ID", str(caught.exception))

    def test_authority_reader_accepts_empty_participant_for_system_conversation(self):
        """群组或系统会话可没有对端 sec_uid，但其空槽位仍须进入顺序 proof。"""

        page = FakePage(
            authority_raw=make_authority_raw(
                2,
                participant_sec_user_ids=["", "合成参与者sec_uid-1"],
            )
        )

        proof = tasks._read_authoritative_conversation_snapshot(page)

        # 线上 32 个会话中有 6 个空 participant。保留空字符串可让原子边界继续
        # 比较完整位置；正式强身份计划会自然忽略空槽，绝不能把它当作目标身份。
        self.assertEqual(
            proof.participant_sec_user_ids,
            ("", "合成参与者sec_uid-1"),
        )

    def test_authority_evaluate_exception_is_redacted_and_fails_closed(self):
        """页面 evaluate 异常正文不能泄漏，且不得降级为 DOM-only 扫描。"""

        page = ScriptedInventoryPage(
            [1],
            authority_error=RuntimeError("页面异常中含合成机密ID"),
        )

        with self.assertRaises(tasks.ConversationSelectionError) as caught:
            tasks._read_authoritative_conversation_snapshot(page)

        self.assertNotIn("合成机密ID", str(caught.exception))

    def test_same_inventory_with_has_more_true_never_authorizes_click(self):
        """四次乃至更多相同 DOM 在 hasMore=true 时仍不是权威终态。"""

        item = FakeConversationItem("非终态目标", stable_index=0)
        page = ScriptedInventoryPage(
            [1],
            authority_states=[make_authority_raw(1, has_more=True)],
        )
        page.pass_items[0][0] = item
        page.all_items = [item]

        with patch.object(tasks, "MAX_INVENTORY_SCAN_ROUNDS", 8):
            with self.assertRaises(tasks.ConversationSelectionError):
                tasks._scan_full_conversation_inventory(page, friend_list_wait_time=1)

        self.assertGreaterEqual(page.authority_read_count, 4)
        self.assertEqual(item.click_count, 0)

    def test_loading_authority_never_authorizes_inventory(self):
        """SDK 或 store 任一仍 loading 时都不能读取中间态库存。"""

        for loading_field in ("sdkIsLoading", "storeIsLoading"):
            with self.subTest(loading_field=loading_field):
                raw_state = make_authority_raw(1)
                raw_state[loading_field] = True
                page = ScriptedInventoryPage(
                    [1],
                    authority_states=[raw_state],
                )
                with patch.object(tasks, "MAX_INVENTORY_SCAN_ROUNDS", 5):
                    with self.assertRaises(tasks.ConversationSelectionError):
                        tasks._scan_full_conversation_inventory(
                            page,
                            friend_list_wait_time=1,
                        )
                self.assertTrue(
                    all(item.click_count == 0 for item in page.all_items)
                )

    def test_terminal_authority_rejects_dom_missing_one_index(self):
        """150 项终态 authority 与仅 149 个 DOM 索引不能被稳定快照掩盖。"""

        page = ScriptedInventoryPage(
            [149],
            authority_states=[make_authority_raw(150)],
        )

        with patch.object(tasks, "MAX_INVENTORY_SCAN_ROUNDS", 6):
            with self.assertRaises(tasks.ConversationSelectionError):
                tasks._scan_full_conversation_inventory(page, friend_list_wait_time=1)

        self.assertTrue(all(item.click_count == 0 for item in page.all_items))

    def test_mid_scan_authority_change_discards_old_index_mapping(self):
        """窗口读取期间切换权威顺序后，最终库存只能包含新 pass 标题。"""

        page = MidScanAuthorityChangePage()

        inventory, _, proof = tasks._scan_full_conversation_inventory(
            page,
            friend_list_wait_time=1,
        )

        self.assertEqual(inventory, {0: "新顺序标题"})
        self.assertEqual(proof.ordered_ids, ("新权威ID",))
        self.assertNotIn("旧顺序标题", inventory.values())
        self.assertTrue(all(item.click_count == 0 for item in page.all_items))

    def test_two_passes_with_same_dom_but_different_id_order_never_match(self):
        """DOM 映射相同也不能覆盖 authority ordered_ids 的顺序变化。"""

        first_order = make_authority_raw(2, ordered_ids=["权威甲", "权威乙"])
        second_order = make_authority_raw(2, ordered_ids=["权威乙", "权威甲"])
        page = ScriptedInventoryPage(
            [2, 2, 2, 2],
            authority_states=[first_order, second_order, first_order, second_order],
        )

        with self.assertRaises(tasks.ConversationSelectionError):
            tasks._scan_full_conversation_inventory(page, friend_list_wait_time=1)

        self.assertEqual(page.pass_index, tasks.MAX_INVENTORY_SCAN_PASSES - 1)
        self.assertTrue(all(item.click_count == 0 for item in page.all_items))

    def test_final_reset_authority_change_invalidates_two_matching_passes(self):
        """两次 pass 一致后，最终回顶若变序仍必须 fail-closed。"""

        stable = make_authority_raw(2, ordered_ids=["权威甲", "权威乙"])
        changed = make_authority_raw(2, ordered_ids=["权威乙", "权威甲"])
        page = ScriptedInventoryPage(
            [2, 2, 2],
            authority_states=[stable, stable, changed],
        )

        with self.assertRaises(tasks.ConversationSelectionError):
            tasks._scan_full_conversation_inventory(page, friend_list_wait_time=1)

        self.assertEqual(page.pass_index, 2)
        self.assertTrue(all(item.click_count == 0 for item in page.all_items))

    def test_forward_inventory_scroll_uses_overlapping_viewports(self):
        """前进步长保持三成视口，并由真实 scrollTop 变化证明已经推进。"""

        page = FakeConversationListPage(
            [],
            right_title="未使用标题",
            pages=[[], [], []],
        )

        tasks._scroll_list_forward(page, page.scroll_handle, page.client_height)

        self.assertEqual(page.scroll_top, 30)

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

    def test_same_identity_aliases_are_merged_into_one_conversation_click(self):
        """同一唯一身份的两个配置别名必须只选择一次并显式覆盖整组。"""

        identity = tasks.FriendIdentity(
            short_id="配置别名甲",
            unique_id="配置别名乙",
            sec_uid="合成参与者sec_uid-0",
            nickname="唯一好友",
            remark_name="唯一好友",
        )
        identity_index = {"唯一好友": {identity}}
        item = FakeConversationItem("唯一好友", stable_index=0)
        page = FakeConversationListPage([item], right_title="唯一好友")

        selections = list(
            tasks.scroll_and_select_user(
                page,
                "别名归并账号",
                ["配置别名甲", "配置别名乙"],
                identity_index=identity_index,
                friend_list_wait_time=1,
                confirmation_timeout=1,
            )
        )

        self.assertEqual(item.click_count, 1)
        self.assertEqual(len(selections), 1)
        self.assertEqual(selections[0].target_symbol, "配置别名甲")
        self.assertEqual(
            selections[0].covered_targets,
            ("配置别名甲", "配置别名乙"),
        )

    def test_twelve_aliases_build_exactly_six_logical_conversations(self):
        """复现线上配置形态：十二个标识成对归并后只能得到六个会话。"""

        inventory = {}
        identity_index = {}
        targets = []
        for stable_index in range(6):
            display_name = f"合成好友{stable_index}"
            first_alias = f"合成别名{stable_index}-甲"
            second_alias = f"合成别名{stable_index}-乙"
            identity = tasks.FriendIdentity(
                short_id=first_alias,
                unique_id=second_alias,
                sec_uid=f"合成强身份-{stable_index}",
                nickname=display_name,
                remark_name=display_name,
            )
            inventory[stable_index] = display_name
            identity_index[display_name] = {identity}
            targets.extend((first_alias, second_alias))

        plan = tasks._build_unique_selection_plan(
            inventory,
            targets,
            identity_index,
        )

        # 计划项数量就是后续允许发生的最大点击/Enter 次数；覆盖数则证明十二个
        # 配置标识都被唯一会话解释，没有通过静默丢弃别名来凑出六次发送。
        self.assertEqual(len(plan), 6)
        self.assertEqual(
            sum(len(entry.match.covered_targets) for entry in plan.values()),
            12,
        )

    def test_alias_that_also_matches_another_conversation_aborts_before_click(self):
        """任一别名落入第二个会话时，整份计划必须零点击失败。"""

        first_identity = tasks.FriendIdentity(
            "配置别名甲", "配置别名乙", "", "好友甲", "好友甲"
        )
        second_identity = tasks.FriendIdentity(
            "配置别名乙", "其他标识", "", "好友乙", "好友乙"
        )
        identity_index = {
            "好友甲": {first_identity},
            "好友乙": {second_identity},
        }
        first = FakeConversationItem("好友甲", stable_index=0)
        second = FakeConversationItem("好友乙", stable_index=1)
        page = FakeConversationListPage(
            [first, second],
            right_title="好友甲",
        )

        with self.assertRaises(tasks.ConversationSelectionError):
            list(
                tasks.scroll_and_select_user(
                    page,
                    "跨会话别名账号",
                    ["配置别名甲", "配置别名乙"],
                    identity_index=identity_index,
                    friend_list_wait_time=1,
                    confirmation_timeout=1,
                )
            )

        self.assertEqual(first.click_count, 0)
        self.assertEqual(second.click_count, 0)

    def test_other_conversation_starting_to_match_aborts_before_any_click(self):
        """原计划后未选中会话开始命中同一别名时，完整重建必须零点击失败。"""

        selected_display = "原唯一会话"
        other_display = "后到冲突会话"
        target_alias = "唯一配置别名"
        selected_identity = tasks.FriendIdentity(
            "",
            target_alias,
            "原强身份",
            selected_display,
            selected_display,
        )
        unrelated_identity = tasks.FriendIdentity(
            "",
            "无关配置标识",
            "无关强身份",
            other_display,
            other_display,
        )
        late_conflicting_identity = tasks.FriendIdentity(
            "",
            target_alias,
            "后到强身份",
            other_display,
            other_display,
        )
        identity_index = IdentityIndexWhoseOtherConversationStartsMatching(
            other_display,
            late_conflicting_identity,
            {
                selected_display: {selected_identity},
                other_display: {unrelated_identity},
            },
        )
        selected_item = FakeConversationItem(selected_display, stable_index=0)
        other_item = FakeConversationItem(other_display, stable_index=1)
        page = FakeConversationListPage(
            [selected_item, other_item],
            right_title=selected_display,
        )

        with self.assertRaises(tasks.ConversationSelectionError):
            list(
                tasks.scroll_and_select_user(
                    page,
                    "全局重验竞态账号",
                    [target_alias],
                    identity_index=identity_index,
                    friend_list_wait_time=1,
                    confirmation_timeout=1,
                )
            )

        # participant 计划在建立时冻结身份，不再二次读取 mutable identity_index。
        self.assertEqual(identity_index.other_read_count, 0)
        self.assertEqual(selected_item.click_count, 0)
        self.assertEqual(other_item.click_count, 0)

    def test_alias_group_change_after_plan_aborts_before_click(self):
        """计划后唯一身份改为只覆盖部分别名时，完整组重验必须拒绝点击。"""

        initial_identity = tasks.FriendIdentity(
            "配置别名甲", "配置别名乙", "", "竞态好友", "竞态好友"
        )
        changed_identity = tasks.FriendIdentity(
            "配置别名甲", "", "变化后安全标识", "竞态好友", "竞态好友"
        )
        identity_index = IdentityIndexWhoseAliasGroupChanges(
            "竞态好友",
            initial_identity,
            changed_identity,
        )
        item = FakeConversationItem("竞态好友", stable_index=0)
        page = FakeConversationListPage([item], right_title="竞态好友")

        with self.assertRaises(tasks.ConversationSelectionError):
            list(
                tasks.scroll_and_select_user(
                    page,
                    "别名竞态账号",
                    ["配置别名甲", "配置别名乙"],
                    identity_index=identity_index,
                    friend_list_wait_time=1,
                    confirmation_timeout=1,
                )
            )

        self.assertEqual(identity_index.read_count, 0)
        self.assertEqual(item.click_count, 0)

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

        self.assertEqual(identity_index.read_count, 0)
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
                    identity_index=None,
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
        overlap_item = FakeConversationItem("跨页重叠锚点", stable_index=2)
        page = FakeConversationListPage(
            [],
            right_title="跨页同名",
            # 两个虚拟窗口共享 index=2，符合生产 70% 视口重叠协议；同名目标仍
            # 分处不同窗口，继续验证首次点击前的全局重名发现能力。
            pages=[
                [first_page_item, overlap_item],
                [overlap_item, later_page_item],
            ],
        )

        with self.assertRaises(tasks.ConversationSelectionError):
            list(
                tasks.scroll_and_select_user(
                    page,
                    "跨页歧义账号",
                    ["跨页同名"],
                    identity_index=None,
                    friend_list_wait_time=1,
                    confirmation_timeout=1,
                )
            )

        self.assertFalse(first_page_item.clicked)
        self.assertFalse(later_page_item.clicked)
        self.assertFalse(overlap_item.clicked)

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
                    identity_index=None,
                    friend_list_wait_time=1,
                    confirmation_timeout=1,
                )
            )

        self.assertFalse(item.clicked)

    def test_overscan_target_waits_until_it_intersects_list_before_single_click(self):
        """目标先仅存在于 overscan 时必须跳过，进入视口后才恰好点击一次。"""

        first_visible_anchor = FakeConversationItem("首屏可见锚点", stable_index=0)
        overscan_target = FakeConversationItem("overscan目标", stable_index=1)
        overscan_target.actionable = False
        visible_target = FakeConversationItem("overscan目标", stable_index=1)
        second_visible_anchor = FakeConversationItem("后屏可见锚点", stable_index=2)
        page = FakeConversationListPage(
            [],
            right_title="overscan目标",
            pages=[
                # index=1 在首屏 DOM 中但不与列表矩形相交；它仍参与库存一致性，
                # 只是不能成为发送搜索的点击候选。
                [first_visible_anchor, overscan_target],
                # 滚动后同一稳定索引真正进入容器。两个窗口共享 index=1，也满足
                # 生产扫描要求的重叠连续性证据。
                [visible_target, second_visible_anchor],
            ],
        )

        selections = list(
            tasks.scroll_and_select_user(
                page,
                "overscan账号",
                ["overscan目标"],
                identity_index=None,
                friend_list_wait_time=1,
                confirmation_timeout=1,
            )
        )

        self.assertEqual(len(selections), 1)
        self.assertIs(selections[0].item, visible_target)
        self.assertEqual(overscan_target.click_count, 0)
        self.assertEqual(visible_target.click_count, 1)
        self.assertEqual(
            sum(
                item.click_count
                for item in (
                    first_visible_anchor,
                    overscan_target,
                    visible_target,
                    second_visible_anchor,
                )
            ),
            1,
        )

    def test_authority_order_change_after_plan_aborts_before_click(self):
        """计划完成后 ordered_ids 变序，即使目标仍在 index=0 也必须零点击。"""

        target = FakeConversationItem("顺序竞态目标", stable_index=0)
        other = FakeConversationItem("顺序竞态旁项", stable_index=1)
        initial = make_authority_raw(2, ordered_ids=["权威目标", "权威旁项"])
        changed = make_authority_raw(2, ordered_ids=["权威旁项", "权威目标"])
        page = FakeConversationListPage(
            [target, other],
            right_title="顺序竞态目标",
            authority_raw=initial,
        )
        original_builder = tasks._build_unique_selection_plan
        build_calls = 0

        def build_plan_then_reorder(*args, **kwargs):
            nonlocal build_calls
            result = original_builder(*args, **kwargs)
            build_calls += 1
            if build_calls == 1:
                # 目标自己的 DOM index/title 刻意保持不变，只替换权威 ID 顺序，
                # 确保拦截来自 proof 而不是候选项的局部标题检查。
                page.authority_raw = changed
            return result

        with patch.object(
            tasks,
            "_build_unique_selection_plan",
            side_effect=build_plan_then_reorder,
        ):
            with self.assertRaises(tasks.ConversationSelectionError):
                list(
                    tasks.scroll_and_select_user(
                        page,
                        "顺序竞态账号",
                        ["顺序竞态目标"],
                        identity_index=None,
                        friend_list_wait_time=1,
                        confirmation_timeout=1,
                    )
                )

        self.assertEqual(target.click_count, 0)
        self.assertEqual(other.click_count, 0)

    def test_click_requires_active_item_and_matching_right_title(self):
        """确认成功必须同时来自被点击元素的当前类与规范化后相同的右侧标题。"""

        item = FakeConversationItem("目标 好友")
        page = FakeConversationListPage([item], right_title=" 目标　好友 ")

        selected = list(
            tasks.scroll_and_select_user(
                page,
                "确认账号",
                ["目标 好友"],
                identity_index=None,
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
            identity_index=None,
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
                    identity_index=None,
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
            stable_index=0,
            authority_proof=make_authority_snapshot(1),
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
            context.lifecycle_events[:3],
            ["add_init_script", "new_page", "goto"],
        )
        self.assertEqual(
            [action for action in chat_input.actions if action == ("press", "Shift+Enter")],
            [("press", "Shift+Enter"), ("press", "Shift+Enter")],
        )

    def test_preinstalled_gate_blocks_unarmed_enter_and_all_followup_phases(self):
        """页面创建前预装门禁；未 arm Enter 的 keydown 到 keyup 均不能传播。"""

        chat_input = FakeChatInput(initial_text="未授权草稿")
        page = FakePage(chat_input=chat_input)
        context = FakeContext(page)

        tasks._preinstall_enter_capture_gate(context)
        self.assertEqual(context.lifecycle_events, ["add_init_script"])
        self.assertIs(context.new_page(), page)
        chat_input.press("Enter")

        self.assertEqual(chat_input.text, "未授权草稿")
        self.assertEqual(
            page.blocked_enter_phases,
            ["keydown", "keypress", "beforeinput", "keyup"],
        )
        self.assertEqual(
            context.lifecycle_events[:2],
            ["add_init_script", "new_page"],
        )

    def test_alias_group_is_submitted_once_and_covers_both_requested_targets(self):
        """一次会话 Enter 清空后，结果应同时覆盖其两个请求别名。"""

        chat_input = FakeChatInput()
        page = FakePage(chat_input=chat_input, right_title="确认好友")
        context = FakeContext(page)
        browser = FakeBrowser(context)
        selected_item = FakeConversationItem("确认好友")
        selected_item.active = True
        selection = tasks.ConfirmedConversation(
            target_symbol="配置别名甲",
            display_name="确认好友",
            item=selected_item,
            covered_targets=("配置别名甲", "配置别名乙"),
            stable_index=0,
            authority_proof=make_authority_snapshot(1),
        )

        with patch.object(
            tasks,
            "scroll_and_select_user",
            return_value=[selection],
        ), patch.object(tasks, "_build_message", return_value="只发送一次"):
            result = tasks.do_user_task(
                browser,
                "别名提交账号",
                [],
                ["配置别名甲", "配置别名乙"],
                runtime_config=TEST_CONFIG,
            )

        self.assertEqual(result.state, tasks.TaskState.SUBMITTED_UNCONFIRMED)
        self.assertEqual(
            result.submitted_targets,
            ("配置别名甲", "配置别名乙"),
        )
        self.assertEqual(result.missing_targets, ())
        self.assertEqual(chat_input.actions.count(("press", "Enter")), 1)
        self.assertTrue(context.closed)

    def test_message_build_state_change_is_rechecked_before_first_type(self):
        """远程消息构建期间标题变化时，返回后必须在零输入状态终止。"""

        chat_input = FakeChatInput()
        page = FakePage(chat_input=chat_input, right_title="确认好友")
        context = FakeContext(page)
        browser = FakeBrowser(context)
        item = FakeConversationItem("确认好友")
        item.active = True
        selection = tasks.ConfirmedConversation(
            "目标一",
            "确认好友",
            item,
            stable_index=0,
            authority_proof=make_authority_snapshot(1),
        )

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

    def test_authority_change_after_typing_blocks_enter(self):
        """消息已输入但 Enter 前 authority 变化时，绝不能按下发送键。"""

        initial_raw = make_authority_raw(1, ordered_ids=["输入前权威ID"])
        changed_raw = make_authority_raw(1, ordered_ids=["输入后权威ID"])
        proof = make_authority_snapshot(1, ordered_ids=["输入前权威ID"])
        chat_input = FakeChatInput()
        page = FakePage(
            chat_input=chat_input,
            right_title="Enter竞态好友",
            authority_raw=initial_raw,
        )
        context = FakeContext(page)
        browser = FakeBrowser(context)
        item = FakeConversationItem("Enter竞态好友", stable_index=0)
        item.active = True
        item.authority_page = page
        selection = tasks.ConfirmedConversation(
            "Enter竞态目标",
            "Enter竞态好友",
            item,
            stable_index=0,
            authority_proof=proof,
        )
        original_typer = tasks._type_multiline_message

        def type_then_change_authority(editor, message):
            original_typer(editor, message)
            # 类型动作已经发生，随后模拟 IM 顺序事件；Enter 前的第二次 authority
            # 读取必须看到变化并保留未发送草稿，不能用旧 proof 执行副作用。
            page.authority_raw = changed_raw

        with patch.object(
            tasks,
            "scroll_and_select_user",
            return_value=[selection],
        ), patch.object(
            tasks,
            "_build_message",
            return_value="只应输入、不应发送",
        ), patch.object(
            tasks,
            "_type_multiline_message",
            side_effect=type_then_change_authority,
        ):
            with self.assertRaises(tasks.ConversationSelectionError):
                tasks.do_user_task(
                    browser,
                    "Enter竞态账号",
                    [],
                    ["Enter竞态目标"],
                    runtime_config={**TEST_CONFIG, "browserTimeout": 1},
                )

        self.assertNotIn(("press", "Enter"), chat_input.actions)
        self.assertTrue(any(action[0] == "type" for action in chat_input.actions))
        self.assertTrue(context.closed)

    def test_keydown_guard_blocks_authority_change_after_final_precheck(self):
        """Python 终检返回后才变序，也必须由页面 capture guard 阻断 Enter。"""

        initial_raw = make_authority_raw(1, ordered_ids=["守卫初始权威ID"])
        changed_raw = make_authority_raw(1, ordered_ids=["守卫变化权威ID"])
        proof = make_authority_snapshot(1, ordered_ids=["守卫初始权威ID"])
        chat_input = FakeChatInput()
        page = FakePage(
            chat_input=chat_input,
            right_title="守卫竞态好友",
            authority_raw=initial_raw,
        )
        context = FakeContext(page)
        browser = FakeBrowser(context)
        item = FakeConversationItem("守卫竞态好友", stable_index=0)
        item.active = True
        selection = tasks.ConfirmedConversation(
            "守卫竞态目标",
            "守卫竞态好友",
            item,
            stable_index=0,
            authority_proof=proof,
        )

        # hook 只在 FakeChatInput.press("Enter") 已被调用、fake window capture 尚未
        # 判定前运行，因此 Python 的所有 Enter 前双读 proof 都仍会看到初始顺序。
        chat_input.before_enter_hook = lambda: setattr(
            page,
            "authority_raw",
            changed_raw,
        )
        with patch.object(
            tasks,
            "scroll_and_select_user",
            return_value=[selection],
        ), patch.object(
            tasks,
            "_build_message",
            return_value="守卫应保留但不得发送的草稿",
        ):
            with self.assertRaises(tasks.ConversationSelectionError):
                tasks.do_user_task(
                    browser,
                    "守卫竞态账号",
                    [],
                    ["守卫竞态目标"],
                    runtime_config={**TEST_CONFIG, "browserTimeout": 1},
                )

        # Playwright 层只尝试一次真实 press，但 capture guard 在站点处理器之前拒绝，
        # 因此 fake 编辑器不会清空；状态已读取一次并在 finally 语义下完成清理。
        self.assertEqual(chat_input.actions.count(("press", "Enter")), 1)
        self.assertEqual(chat_input.text, "守卫应保留但不得发送的草稿")
        self.assertEqual(page.enter_guard_cleanup_count, 1)
        self.assertIsNone(page.enter_guard)
        self.assertTrue(context.closed)
        self.assertIn("keyup", page.blocked_enter_phases)
        self.assertIn("keypress", page.blocked_enter_phases)

        guard_script = chat_input.last_guard_script
        self.assertIn('await remote.get(".")', guard_script)
        self.assertIn("link.isLoading", guard_script)
        self.assertIn("gate.arm(validator)", guard_script)
        self.assertNotIn("addEventListener", guard_script)
        self.assertIn("event.isTrusted", guard_script)
        # remote factory 的 await 只能发生在安装阶段；keydown proof 到事件放行之间
        # 不得再次让出事件循环。
        self.assertNotIn("await", guard_script[guard_script.index("const validator") :])

        init_script = context.init_scripts[0]
        self.assertIn("preinstallEnterCaptureGate", init_script)
        for phase in ("keydown", "keypress", "beforeinput", "keyup"):
            self.assertIn(f'addEventListener("{phase}"', init_script)
        self.assertIn("event.preventDefault()", init_script)
        self.assertIn("event.stopImmediatePropagation()", init_script)

    def test_do_user_task_rejects_missing_or_nonterminal_proof_before_editor(self):
        """兼容构造或非终态 proof 都不得让真实发送路径静默退回 DOM-only。"""

        cases = (
            (
                "缺少 proof",
                tasks.ConfirmedConversation(
                    "无证明目标",
                    "确认好友",
                    FakeConversationItem("确认好友", stable_index=0),
                ),
            ),
            (
                "proof 非终态",
                tasks.ConfirmedConversation(
                    "非终态目标",
                    "确认好友",
                    FakeConversationItem("确认好友", stable_index=0),
                    stable_index=0,
                    authority_proof=make_authority_snapshot(1, has_more=True),
                ),
            ),
        )
        for label, selection in cases:
            with self.subTest(label=label):
                selection.item.active = True
                chat_input = FakeChatInput()
                page = FakePage(chat_input=chat_input, right_title="确认好友")
                context = FakeContext(page)
                browser = FakeBrowser(context)
                with patch.object(
                    tasks,
                    "scroll_and_select_user",
                    return_value=[selection],
                ), patch.object(tasks, "_build_message") as build_message:
                    with self.assertRaises(tasks.ConversationSelectionError):
                        tasks.do_user_task(
                            browser,
                            "证明门禁账号",
                            [],
                            [selection.target_symbol],
                            runtime_config={**TEST_CONFIG, "browserTimeout": 1},
                        )

                build_message.assert_not_called()
                self.assertEqual(chat_input.actions, [])
                self.assertNotIn(
                    (tasks.CHAT_EDITOR_SELECTOR, 1),
                    page.waited_selectors,
                )
                self.assertTrue(context.closed)

    def test_atomic_click_rejects_detached_or_hidden_item(self):
        """DOM 原子点击不得绕过连接状态或 actionability 门禁。"""

        proof = make_authority_snapshot(1)
        for attribute in ("connected", "actionable"):
            with self.subTest(attribute=attribute):
                page = FakeConversationListPage(
                    [FakeConversationItem("原子目标", stable_index=0)],
                    right_title="原子目标",
                )
                item = page.pages[0][0]
                item.authority_page = page
                setattr(item, attribute, False)
                with self.assertRaises(tasks.ConversationSelectionError):
                    tasks._click_conversation_at_authority_boundary(
                        item,
                        0,
                        "原子目标",
                        proof,
                    )
                self.assertEqual(item.click_count, 0)

    def test_existing_draft_aborts_before_typing_or_enter(self):
        """编辑器内已有用户草稿时不能追加模板，更不能按 Enter。"""

        chat_input = FakeChatInput(initial_text="用户尚未发送的旧草稿")
        page = FakePage(chat_input=chat_input, right_title="确认好友")
        context = FakeContext(page)
        browser = FakeBrowser(context)
        item = FakeConversationItem("确认好友")
        item.active = True
        selection = tasks.ConfirmedConversation(
            "目标一",
            "确认好友",
            item,
            stable_index=0,
            authority_proof=make_authority_snapshot(1),
        )

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
        script = chat_input.last_content_state_script
        self.assertIn("standardLeaves.length === 1", script)
        self.assertIn("standardZeroWidths.length === 1", script)
        self.assertIn('zeroWidth.getAttribute("data-slate-length") === "0"', script)

    def test_live_custom_zero_width_empty_editor_is_accepted_atomically(self):
        """生产 data-node/data-string/U+200B 空态应通过，且脚本不回传正文。"""

        chat_input = FakeChatInput(
            initial_text="\u200b",
            editor_structure="custom",
        )
        page = FakePage(chat_input=chat_input)

        selected_editor = tasks._get_unique_empty_editor(page, 1)

        self.assertIs(selected_editor, chat_input)
        script = chat_input.last_content_state_script
        self.assertIn("readEditorContentState", script)
        self.assertIn('span.childNodes[0].nodeValue === "\\u200b"', script)
        self.assertIn('span.hasAttribute("data-enter")', script)
        self.assertIn('span.hasAttribute("data-string")', script)

    def test_live_custom_editor_returns_to_exact_empty_marker_after_enter(self):
        """自定义编辑器完整发送路径应从 U+200B 空态出发并回到同一空态。"""

        chat_input = FakeChatInput(
            initial_text="\u200b",
            editor_structure="custom",
        )
        page = FakePage(chat_input=chat_input, right_title="自定义编辑器好友")
        context = FakeContext(page)
        browser = FakeBrowser(context)
        item = FakeConversationItem("自定义编辑器好友", stable_index=0)
        item.active = True
        selection = tasks.ConfirmedConversation(
            "自定义编辑器目标",
            "自定义编辑器好友",
            item,
            stable_index=0,
            authority_proof=make_authority_snapshot(1),
        )

        with patch.object(
            tasks,
            "scroll_and_select_user",
            return_value=[selection],
        ), patch.object(tasks, "_build_message", return_value="一次可信提交"):
            result = tasks.do_user_task(
                browser,
                "自定义编辑器账号",
                [],
                ["自定义编辑器目标"],
                runtime_config=TEST_CONFIG,
            )

        self.assertEqual(result.state, tasks.TaskState.SUBMITTED_UNCONFIRMED)
        self.assertEqual(chat_input.actions.count(("press", "Enter")), 1)
        self.assertEqual(chat_input.text, "\u200b")
        self.assertTrue(context.closed)

    def test_whitespace_draft_is_not_normalized_into_empty_editor(self):
        """用户只输入空格也属于旧草稿，不能被 trim/norm 后覆盖。"""

        chat_input = FakeChatInput(initial_text="   ")
        page = FakePage(chat_input=chat_input)

        with self.assertRaises(tasks.EditorSafetyError):
            tasks._get_unique_empty_editor(page, 1)

        self.assertEqual(chat_input.actions, [])

    def test_unknown_or_non_text_editor_content_fails_closed(self):
        """无标记根与 void/附件态都不得被“无 string”误判为空。"""

        for structure in ("markerless", "void", "standard_multiple_empty_blocks"):
            with self.subTest(structure=structure):
                chat_input = FakeChatInput(editor_structure=structure)
                page = FakePage(chat_input=chat_input)

                with self.assertRaises(tasks.EditorSafetyError):
                    tasks._get_unique_empty_editor(page, 1)

                self.assertEqual(chat_input.actions, [])

    def test_editor_clear_confirmation_accepts_live_custom_empty_state(self):
        """Enter 后编辑器恢复生产 U+200B 空态时，应确认清空而不重复按键。"""

        chat_input = FakeChatInput(
            initial_text="\u200b",
            editor_structure="custom",
        )
        page = FakePage(chat_input=chat_input)

        tasks._wait_for_editor_cleared(page, chat_input, timeout_ms=1)

        self.assertEqual(chat_input.actions, [])

    def test_enter_is_not_retried_when_editor_does_not_clear(self):
        """Enter 后文本未清空属于失败；即使等待超时也只能按一次 Enter。"""

        chat_input = FakeChatInput(clear_after_enter=False)
        page = FakePage(chat_input=chat_input, right_title="确认好友")
        context = FakeContext(page)
        browser = FakeBrowser(context)
        item = FakeConversationItem("确认好友")
        item.active = True
        selection = tasks.ConfirmedConversation(
            "目标一",
            "确认好友",
            item,
            stable_index=0,
            authority_proof=make_authority_snapshot(1),
        )

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
        selection = tasks.ConfirmedConversation(
            "目标一",
            "确认好友",
            item,
            stable_index=0,
            authority_proof=make_authority_snapshot(1),
        )

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
