"""抖音聊天任务的同步编排与保守结果建模。

本模块刻意不在导入阶段读取账号配置或启动浏览器。真实运行所需的配置只在
``runTasks`` / ``do_user_task`` 被调用后读取，因此测试、配置校验和其他模块导入
都不会意外触发账号操作。

线上只读探测已经获得会话切换的双重证据：被点击的列表项会增加当前会话类名，
右侧标题也会变成该列表项的显示名。因此代码在输入前必须同时验证这两项证据，
任何身份歧义、旧草稿或 DOM 状态不一致都会终止当前账号。页面仍没有可靠的服务端
送达回执，所以只有 Enter 后编辑器确实清空时才记为“已提交但未确认”，绝不把
点击、输入或按键本身误报为发送成功。
"""

from __future__ import annotations

import re
import sys
import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    MutableMapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from core.browser import get_browser
from utils import norm
from utils.config import get_config, get_user_data
from utils.logger import setup_logger

if TYPE_CHECKING:
    # Playwright 是运行期的可选重依赖。仅在类型检查阶段导入，保证没有安装
    # Chromium/Playwright 的配置机和纯单元测试仍可安全导入本模块。
    from playwright.sync_api import Response


# 导入模块时不创建日志目录或文件；真实处理器只在 runTasks 读取完环境配置后安装。
logger = logging.getLogger("app")

CONVERSATION_ITEM_SELECTOR = ".conversationConversationItemwrapper"
CONVERSATION_TITLE_SELECTOR = ".conversationConversationItemtitle"
CONVERSATION_LIST_SELECTOR = ".conversationConversationListwrapper"
CONVERSATION_INDEX_ANCESTOR_SELECTOR = "xpath=ancestor::*[@data-index][1]"
CURRENT_CONVERSATION_CLASS = "conversationConversationItemcurConversation"
RIGHT_PANEL_TITLE_SELECTOR = ".RightPanelHeadertitle"
CHAT_EDITOR_CONTAINER_SELECTOR = ".messageEditorimChatEditorContainer"
# 编辑器容器本身并不是可输入节点。限定 Slate 标记与 contenteditable=true，并在
# 输入前额外检查 count==1，防止页面同时保留隐藏编辑器时把内容写进错误节点。
CHAT_EDITOR_SELECTOR = (
    f'{CHAT_EDITOR_CONTAINER_SELECTOR} '
    '[data-slate-editor="true"][contenteditable="true"]'
)
# Slate 的 placeholder 与 zero-width 占位节点可能出现在编辑器的 innerText 中，但
# 它们不是用户草稿。leaf 只用于证明 Slate 结构仍存在，真实文本则仅从 Slate
# 专用的 data-slate-string 节点读取，显式排除上述两类占位内容。
SLATE_TEXT_LEAF_SELECTOR = '[data-slate-leaf="true"]'
SLATE_TEXT_STRING_SELECTOR = '[data-slate-string="true"]'

# 页面渲染聊天任务不需要图片、音视频和字体。只拦截这些明确无关的资源，文档、
# 脚本、XHR、fetch 与样式表仍放行，避免为了节省资源破坏站点核心逻辑。
BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})
USER_INFO_URL_FRAGMENT = "aweme/v1/web/im/user/info"
DOM_CONFIRM_POLL_INTERVAL_MS = 100
MAX_INVENTORY_SCAN_ROUNDS = 500


@dataclass(frozen=True)
class FriendIdentity:
    """某个好友响应中可用于匹配的规范化身份字段。"""

    short_id: str
    unique_id: str
    sec_uid: str
    nickname: str
    remark_name: str

    def candidates(self) -> Tuple[str, ...]:
        """按用户更可能配置的顺序返回非空、去重后的匹配值。"""

        values: List[str] = []
        for value in (
            self.short_id,
            self.unique_id,
            self.sec_uid,
            self.nickname,
            self.remark_name,
        ):
            if value and value not in values:
                values.append(value)
        return tuple(values)


# 同一个规范化显示名可能对应多条好友身份。索引必须保留完整集合，不能让后到的
# 响应覆盖先到的身份；只有集合中恰好存在一个身份时，调用方才允许继续匹配。
IdentityIndex = Mapping[str, Set[FriendIdentity]]
MutableIdentityIndex = MutableMapping[str, Set[FriendIdentity]]


@dataclass(frozen=True)
class _TargetAliasMatch:
    """一个页面会话对配置标识的完整、不可变匹配快照。

    ``covered_targets`` 保存同一好友身份命中的全部配置别名；``identity`` 则保存
    计划建立时唯一的好友身份。点击前会同时比较这两个字段，既能发现别名组增减，
    也能发现身份对象被另一条响应替换但恰好仍命中同样别名的竞态。
    """

    covered_targets: Tuple[str, ...]
    identity: Optional[FriendIdentity] = field(repr=False)


@dataclass(frozen=True)
class _SelectionPlanEntry:
    """全量库存中一个允许点击的唯一逻辑会话。"""

    display_name: str
    match: _TargetAliasMatch


@dataclass(frozen=True)
class ConfirmedConversation:
    """已通过双重 DOM 证据确认的会话选择结果。

    ``target_symbol`` 保留旧版单目标调用方使用的首个配置标识；
    ``covered_targets`` 显式列出该唯一会话覆盖的全部配置别名。``item`` 保留被
    点击的同一个 Locator，使输入前和 Enter 前能够再次核验它仍是当前会话；该
    Locator 不参与业务比较，也不应写入日志，避免其内部信息暴露。
    """

    target_symbol: str
    display_name: str
    item: Any = field(compare=False, repr=False)
    covered_targets: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """让旧的单目标构造方式自动得到一致的别名覆盖集合。"""

        if not self.covered_targets:
            object.__setattr__(self, "covered_targets", (self.target_symbol,))
            return
        if self.covered_targets[0] != self.target_symbol:
            raise ValueError("首个兼容目标必须与别名覆盖集合的首项一致")


class ConversationSelectionError(RuntimeError):
    """会话身份或切换证据不唯一时的 fail-closed 异常。"""


class EditorSafetyError(RuntimeError):
    """编辑器不唯一或包含旧草稿时的 fail-closed 异常。"""


class SubmissionConfirmationError(RuntimeError):
    """Enter 后编辑器没有清空，因而不能记录提交的异常。"""


class TaskState(str, Enum):
    """账号任务的保守终态。"""

    SUBMITTED_UNCONFIRMED = "submitted_unconfirmed"
    PARTIAL_FAILURE = "partial_failure"
    FAILED = "failed"


@dataclass(frozen=True)
class TaskResult:
    """单账号任务结果，不把无证据的页面行为描述成发送成功。"""

    username: str
    state: TaskState
    requested_targets: Tuple[str, ...]
    submitted_targets: Tuple[str, ...] = ()
    missing_targets: Tuple[str, ...] = ()
    error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        """表示任务流程完整执行；消息本身仍属于未确认状态。"""

        return self.state == TaskState.SUBMITTED_UNCONFIRMED

    @classmethod
    def failed(
        cls,
        username: str,
        requested_targets: Sequence[str],
        error: BaseException,
    ) -> "TaskResult":
        """把账号级异常转换为可汇总、且不包含 Cookie 的失败结果。"""

        return cls(
            username=username,
            state=TaskState.FAILED,
            requested_targets=tuple(requested_targets),
            missing_targets=tuple(requested_targets),
            error=f"{type(error).__name__}: {error}",
        )


class TaskBatchError(RuntimeError):
    """至少一个账号或浏览器清理阶段失败时抛出的整体失败异常。"""

    def __init__(self, results: Sequence[TaskResult]):
        self.results = tuple(results)
        failed_names = [result.username for result in self.results if not result.succeeded]
        super().__init__(
            f"{len(failed_names)} 个任务未完整执行：{', '.join(failed_names)}"
        )


def _normalize_identity_value(value: Any) -> str:
    """把响应中的可选标识安全转换为 ``norm`` 可处理的字符串。"""

    if value is None:
        return ""
    return norm(str(value))


def retry_operation(
    name: str,
    operation: Callable[..., Any],
    retries: int = 3,
    delay: float = 2,
    *args: Any,
    sleep_fn: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> Any:
    """重试尚未产生外部副作用的同步操作。

    该帮助函数用于页面导航和响应体读取，不用于重试“按 Enter 发送”这类可能已经
    发生副作用的动作，以免网络超时后重复给好友发消息。
    """

    if retries < 1:
        raise ValueError("retries 必须至少为 1")

    for attempt in range(1, retries + 1):
        try:
            return operation(*args, **kwargs)
        except Exception as exc:
            if attempt >= retries:
                logger.error("%s 失败，已达到最大尝试次数 %s：%s", name, retries, exc)
                raise
            logger.warning(
                "%s 第 %s/%s 次尝试失败，将在 %.3f 秒后重试：%s",
                name,
                attempt,
                retries,
                delay,
                exc,
            )
            sleep_fn(max(delay, 0))

    # 上面的循环要么返回、要么抛出；此分支只用于帮助静态类型检查器收窄类型。
    raise AssertionError("不可达的重试状态")


def handle_response(
    response: "Response",
    identity_index: MutableIdentityIndex,
    retries: int = 3,
    retry_delay: float = 0.5,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """解析好友身份接口，并写入当前账号私有的身份索引。

    Playwright 可能先触发 response 事件、稍后才允许读取完整响应体，因此
    ``response.json()`` 使用有限重试。解析失败只影响这次辅助身份数据，不让事件
    回调异常直接击穿整个页面任务；后续仍可用页面显示名与配置目标直接匹配。
    """

    if USER_INFO_URL_FRAGMENT not in str(getattr(response, "url", "")):
        return False

    try:
        payload = retry_operation(
            "读取好友身份响应",
            response.json,
            retries=retries,
            delay=retry_delay,
            sleep_fn=sleep_fn,
        )
    except Exception as exc:
        logger.warning("好友身份响应在重试后仍无法解析，保留直接名称匹配：%s", exc)
        return False

    if not isinstance(payload, Mapping):
        logger.warning("好友身份响应不是 JSON 对象，已忽略")
        return False
    items = payload.get("data", [])
    if not isinstance(items, list):
        logger.warning("好友身份响应的 data 不是数组，已忽略")
        return False

    updated = False
    for item in items:
        if not isinstance(item, Mapping):
            continue

        nickname = _normalize_identity_value(item.get("nickname"))
        raw_remark_name = _normalize_identity_value(item.get("remark_name"))
        # 接口常以空字符串表示“没有备注”。必须在规范化后判断空值，否则空备注
        # 会成为索引键，聊天列表显示的昵称反而无法关联到抖音号。
        remark_name = raw_remark_name or nickname
        identity = FriendIdentity(
            short_id=_normalize_identity_value(item.get("short_id")),
            unique_id=_normalize_identity_value(item.get("unique_id")),
            sec_uid=_normalize_identity_value(item.get("sec_uid")),
            nickname=nickname,
            remark_name=remark_name,
        )

        # 页面可能显示备注名，也可能显示昵称。集合保留同名下的每一条不同身份，
        # 不能使用简单赋值让后到响应覆盖先到响应，否则同名好友会被误认为唯一。
        # 空键没有匹配意义，且可能把多条无昵称记录错误地合并，因此直接跳过。
        for display_name in (identity.remark_name, identity.nickname):
            if display_name:
                identity_index.setdefault(display_name, set()).add(identity)
                updated = True
    return updated


def _match_target_aliases(
    display_name: str,
    targets: Iterable[str],
    identity_index: Optional[IdentityIndex] = None,
    *,
    allow_direct_display_match: bool = False,
) -> Optional[_TargetAliasMatch]:
    """返回一个唯一会话命中的全部配置别名及身份快照。

    配置可能同时使用同一好友的短号、抖音号等多个标识。只有显示名在身份索引中
    恰好对应一个 ``FriendIdentity`` 时，才允许把该身份命中的多个标识归并为一个
    逻辑会话。无身份数据时的显示名直配仍须由已完成全局 DOM 盘点的调用方显式
    开启。返回值不包含页面 Locator，也不会被写入日志。
    """

    normalized_name = _normalize_identity_value(display_name)
    normalized_targets: List[str] = []
    seen_targets: Set[str] = set()
    for target in targets:
        normalized = _normalize_identity_value(target)
        if normalized and normalized not in seen_targets:
            seen_targets.add(normalized)
            normalized_targets.append(normalized)

    index = identity_index if identity_index is not None else {}
    identities = index.get(normalized_name)
    if identities is not None:
        # 零个或多个身份都不能证明当前 DOM 会话属于哪位好友；即使其中只有一条
        # 身份命中配置，也不能从同名 DOM 推断它就是被点击的那一条。
        if len(identities) != 1:
            return None
        identity = next(iter(identities))
        identity_candidates = set(identity.candidates())
        covered_targets = tuple(
            target for target in normalized_targets if target in identity_candidates
        )
        if covered_targets:
            return _TargetAliasMatch(covered_targets, identity)
        return None

    if allow_direct_display_match and normalized_name in seen_targets:
        return _TargetAliasMatch((normalized_name,), None)
    return None


def checkTargetName(
    targetName: str,
    targets: Iterable[str],
    identity_index: Optional[IdentityIndex] = None,
    *,
    allow_direct_display_match: bool = False,
) -> Optional[str]:
    """把页面显示名解析为单个配置目标，并保留旧版兼容语义。

    新发送计划使用 ``_match_target_aliases`` 归并同一身份的多个别名；这个公开旧
    函数仍只在恰好命中一个标识时返回字符串，避免既有单目标调用方悄然改变类型
    或误把多别名结果当成任意一个目标。
    """

    match = _match_target_aliases(
        targetName,
        targets,
        identity_index,
        allow_direct_display_match=allow_direct_display_match,
    )
    if match is None or len(match.covered_targets) != 1:
        return None
    return match.covered_targets[0]


def _wait_for_page(page: Any, milliseconds: int) -> None:
    """通过 Playwright 同步等待泵送页面事件，而不是使用固定 ``sleep``。"""

    if milliseconds > 0:
        page.wait_for_timeout(milliseconds)


def _handle_lightweight_route(route: Any) -> None:
    """拦截明确不参与聊天逻辑的重资源，其余请求全部放行。"""

    request = getattr(route, "request", None)
    resource_type = getattr(request, "resource_type", "")
    if resource_type in BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def configure_browser_context(
    context: Any, runtime_config: Mapping[str, Any]
) -> None:
    """给账号上下文统一设置超时与低资源路由策略。

    正式任务和只读预检共用这一个入口，避免预检通过的浏览器策略与定时任务实际
    策略不一致。该函数只设置上下文，不创建页面、点击会话或输入消息。
    """

    context.set_default_navigation_timeout(runtime_config["browserTimeout"])
    context.set_default_timeout(runtime_config["browserTimeout"])
    if runtime_config.get("blockBrowserResources", True):
        context.route("**/*", _handle_lightweight_route)


def _conversation_selection_matches(
    page: Any,
    item: Any,
    expected_display_name: str,
    probe_timeout_ms: int = DOM_CONFIRM_POLL_INTERVAL_MS,
) -> bool:
    """一次性验证“原列表项选中 + 右侧标题一致”两项会话证据。

    标题比较统一经过 Python 侧 ``norm``，与好友列表和配置匹配规则完全一致，避免
    全角字符、不可见空格或连续空白让两个视觉上近似的名字被错误地视为同一人。
    右侧标题也必须唯一；零个或多个标题节点都属于页面状态不确定。
    """

    bounded_probe_timeout = max(1, int(probe_timeout_ms))
    item_classes = set(
        (
            item.get_attribute(
                "class",
                timeout=bounded_probe_timeout,
            )
            or ""
        ).split()
    )
    if CURRENT_CONVERSATION_CLASS not in item_classes:
        return False

    right_title = page.locator(RIGHT_PANEL_TITLE_SELECTOR)
    if right_title.count() != 1:
        return False
    return (
        _normalize_identity_value(
            right_title.inner_text(timeout=bounded_probe_timeout)
        )
        == expected_display_name
    )


def _wait_for_confirmed_conversation(
    page: Any,
    item: Any,
    expected_display_name: str,
    timeout_ms: int,
) -> None:
    """有限等待被点击会话取得双重证据，超时后以异常终止账号。

    使用真实单调时钟约束总时限，并为每次 Locator 读取传入不超过剩余预算一半的
    短 timeout。这样元素脱离时不会让一次默认 120 秒等待嵌套进外层轮询。DOM 在
    切换期间短暂重建可在剩余时限内重查，但绝不降级为直接输入。
    """

    bounded_timeout = max(int(timeout_ms), 0)
    deadline = time.monotonic() + (bounded_timeout / 1000)

    while True:
        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        # class 与标题是两个可能等待的读取动作，各自最多使用一半剩余预算；100ms
        # 上限可让页面事件快速轮转，也使实际总耗时保持在调用方 timeout 附近。
        probe_timeout = max(
            1,
            min(DOM_CONFIRM_POLL_INTERVAL_MS, max(remaining_ms // 2, 1)),
        )
        try:
            if _conversation_selection_matches(
                page,
                item,
                expected_display_name,
                probe_timeout_ms=probe_timeout,
            ):
                return
        except Exception:
            # 页面切换时 Locator 可能短暂失效。这里不记录 DOM、标题或异常正文，
            # 既避免隐私泄漏，也确保只有恢复出完整双证据时才会继续。
            pass

        remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
        if remaining_ms <= 0:
            break
        _wait_for_page(
            page,
            min(DOM_CONFIRM_POLL_INTERVAL_MS, remaining_ms),
        )

    raise ConversationSelectionError(
        "点击会话后未能同时确认当前项状态与右侧标题，已在输入前终止"
    )


def _read_stable_conversation_index(
    item: Any,
    timeout_ms: int = DOM_CONFIRM_POLL_INTERVAL_MS,
) -> int:
    """读取会话项最近 ``[data-index]`` 祖先上的稳定非负列表索引。

    线上只读探测确认 ``conversationConversationItemwrapper`` 自身只有 ``data-e2e``，
    最近的直接父 div 才携带虚拟列表 ``data-index``。使用最近祖先而不依赖固定父级
    层数，可容忍无业务含义的包装层；索引缺失、重复或格式异常都不能降级为标题
    猜测，因为预扫描需要靠它区分滚动过程中反复出现的同一 DOM 项。
    """

    indexed_ancestor = item.locator(CONVERSATION_INDEX_ANCESTOR_SELECTOR)
    if indexed_ancestor.count() != 1:
        raise ConversationSelectionError(
            "会话项缺少唯一的稳定 data-index 祖先，无法完成安全预扫描"
        )
    raw_index = indexed_ancestor.get_attribute(
        "data-index",
        timeout=max(1, int(timeout_ms)),
    )
    if raw_index is None or re.fullmatch(r"0|[1-9]\d*", raw_index) is None:
        raise ConversationSelectionError(
            "会话项 data-index 不是非负整数，无法证明列表身份稳定"
        )
    return int(raw_index)


def _read_conversation_item(
    item: Any,
    timeout_ms: int = DOM_CONFIRM_POLL_INTERVAL_MS,
) -> Tuple[int, str]:
    """以短时读取取得一个会话项的稳定索引和规范化显示名。"""

    stable_index = _read_stable_conversation_index(item, timeout_ms)
    title = item.locator(CONVERSATION_TITLE_SELECTOR).inner_text(
        timeout=max(1, int(timeout_ms)),
    )
    display_name = _normalize_identity_value(title)
    if not display_name:
        raise ConversationSelectionError(
            "会话项标题为空，无法建立稳定索引与身份的映射"
        )
    return stable_index, display_name


def _get_conversation_list_handle(page: Any) -> Any:
    """取得唯一滚动容器句柄；缺失时不允许继续到点击阶段。"""

    list_locator = page.locator(CONVERSATION_LIST_SELECTOR)
    if list_locator.count() != 1:
        raise ConversationSelectionError(
            "好友列表滚动容器不是唯一元素，无法完成全列表预扫描"
        )
    handle = list_locator.element_handle()
    if handle is None:
        raise ConversationSelectionError(
            "好友列表滚动容器不可用，无法完成全列表预扫描"
        )
    return handle


def _read_list_metrics(page: Any, list_handle: Any) -> Tuple[float, float, float]:
    """读取并校验虚拟列表滚动指标，供“已到底”证据使用。"""

    raw_metrics = page.evaluate(
        """(element) => ({
            scrollTop: element.scrollTop,
            clientHeight: element.clientHeight,
            scrollHeight: element.scrollHeight,
        })""",
        list_handle,
    )
    if not isinstance(raw_metrics, Mapping):
        raise ConversationSelectionError("好友列表滚动指标格式异常，无法证明已扫描到底")
    try:
        scroll_top = float(raw_metrics["scrollTop"])
        client_height = float(raw_metrics["clientHeight"])
        scroll_height = float(raw_metrics["scrollHeight"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConversationSelectionError(
            "好友列表滚动指标缺失或不是数值，无法证明已扫描到底"
        ) from exc

    if (
        scroll_top < 0
        or client_height <= 0
        or scroll_height < client_height
        or scroll_top > (scroll_height - client_height + 1)
    ):
        raise ConversationSelectionError("好友列表滚动指标越界，无法证明预扫描完整")
    return scroll_top, client_height, scroll_height


def _is_list_bottom(metrics: Tuple[float, float, float]) -> bool:
    """允许 1px 浏览器舍入误差地判断滚动容器是否到达底部。"""

    scroll_top, client_height, scroll_height = metrics
    return scroll_top + client_height >= scroll_height - 1


def _scroll_list_forward(page: Any, list_handle: Any, client_height: float) -> None:
    """向前滚动一个视口，并要求滚动位置确实推进。"""

    before, _, _ = _read_list_metrics(page, list_handle)
    new_position = page.evaluate(
        """([element, step]) => {
            element.scrollTop = Math.min(
                element.scrollTop + step,
                Math.max(element.scrollHeight - element.clientHeight, 0),
            );
            return element.scrollTop;
        }""",
        [list_handle, max(client_height, 1)],
    )
    try:
        after = float(new_position)
    except (TypeError, ValueError) as exc:
        raise ConversationSelectionError("好友列表滚动结果异常，无法继续安全预扫描") from exc
    if after <= before:
        raise ConversationSelectionError(
            "好友列表尚未到底但滚动位置没有推进，预扫描无法证明完整"
        )


def _reset_list_to_top(
    page: Any,
    list_handle: Any,
    wait_time_ms: int,
) -> None:
    """把列表回到顶部并验证结果，确保发送阶段从完整库存起点开始。"""

    reset_position = page.evaluate(
        """(element) => {
            element.scrollTop = 0;
            return element.scrollTop;
        }""",
        list_handle,
    )
    try:
        reset_value = float(reset_position)
    except (TypeError, ValueError) as exc:
        raise ConversationSelectionError("好友列表回顶结果异常，已禁止进入发送阶段") from exc
    if reset_value > 1:
        raise ConversationSelectionError("好友列表未能回到顶部，已禁止进入发送阶段")
    _wait_for_page(page, wait_time_ms)
    top, _, _ = _read_list_metrics(page, list_handle)
    if top > 1:
        raise ConversationSelectionError("好友列表回顶后位置反弹，已禁止进入发送阶段")


def _record_visible_inventory(
    page: Any,
    inventory: MutableMapping[int, str],
) -> int:
    """只读记录当前可见项，并拒绝索引重复或跨轮映射冲突。"""

    visible_items = page.locator(CONVERSATION_ITEM_SELECTOR).all()
    round_indices: Set[int] = set()
    added_count = 0
    for item in visible_items:
        stable_index, display_name = _read_conversation_item(item)
        if stable_index in round_indices:
            raise ConversationSelectionError(
                "同一轮出现重复 data-index，会话库存身份不唯一"
            )
        round_indices.add(stable_index)
        previous_name = inventory.get(stable_index)
        if previous_name is not None and previous_name != display_name:
            raise ConversationSelectionError(
                "同一 data-index 在滚动过程中映射到不同标题，会话库存不稳定"
            )
        if previous_name is None:
            inventory[stable_index] = display_name
            added_count += 1
    return added_count


def _scan_full_conversation_inventory(
    page: Any,
    friend_list_wait_time: int,
) -> Tuple[Dict[int, str], Any]:
    """在任何点击前完整只读扫描列表、证明到底，并验证成功回顶。

    到达底部后必须再等待并得到一轮“索引映射与 scrollHeight 均不变化”的相同
    快照，避免把正在异步追加数据的暂时底部误认为完整列表。扫描轮数有硬上限；
    超限、无法滚动、索引不稳定或回顶失败一律 fail-closed。
    """

    list_handle = _get_conversation_list_handle(page)
    # 首次通常已在顶部，但上一条消息可能让虚拟列表重排并保留中间滚动位置。
    # 每轮库存都从经过验证的顶部开始，data-index 只在本轮只读扫描内作为位置键。
    _reset_list_to_top(page, list_handle, friend_list_wait_time)
    inventory: Dict[int, str] = {}
    previous_bottom_signature: Optional[Tuple[Tuple[Tuple[int, str], ...], float]] = None

    for _ in range(MAX_INVENTORY_SCAN_ROUNDS):
        _record_visible_inventory(page, inventory)
        metrics = _read_list_metrics(page, list_handle)
        if _is_list_bottom(metrics):
            signature = (tuple(sorted(inventory.items())), metrics[2])
            if signature == previous_bottom_signature:
                _reset_list_to_top(page, list_handle, friend_list_wait_time)
                return inventory, list_handle
            previous_bottom_signature = signature
            _wait_for_page(page, friend_list_wait_time)
            continue

        previous_bottom_signature = None
        _scroll_list_forward(page, list_handle, metrics[1])
        _wait_for_page(page, friend_list_wait_time)

    raise ConversationSelectionError(
        "好友列表预扫描超过安全轮数，无法证明已经完整到达底部"
    )


def _build_unique_selection_plan(
    inventory: Mapping[int, str],
    targets: Sequence[str],
    identity_index: Optional[IdentityIndex],
) -> Dict[int, _SelectionPlanEntry]:
    """在第一次点击前证明每个配置标识恰好对应一个全局稳定会话。

    全列表中相同规范化标题若落在多个 ``data-index`` 上，相关标题不会进入候选。
    同一个唯一好友身份命中的多个配置别名会归入同一计划项；一个别名被不同会话
    命中、身份接口同名多身份、别名缺失或目标规范化重复都会让整份计划在首次点击
    前失败，避免“先给部分好友发送，后面才发现歧义”。
    """

    normalized_targets = tuple(_normalize_identity_value(target) for target in targets)
    if not normalized_targets or len(set(normalized_targets)) != len(normalized_targets):
        raise ConversationSelectionError("配置目标为空或规范化后重复，无法建立唯一发送计划")

    display_to_indices: Dict[str, Set[int]] = {}
    for stable_index, display_name in inventory.items():
        display_to_indices.setdefault(display_name, set()).add(stable_index)

    matches_by_target: Dict[str, List[int]] = {
        target: [] for target in normalized_targets
    }
    candidate_plan: Dict[int, _SelectionPlanEntry] = {}
    for display_name, stable_indices in display_to_indices.items():
        # 全局同名即使接口暂时只返回一个身份，也无法证明哪个索引对应它，全部跳过。
        if len(stable_indices) != 1:
            continue
        match = _match_target_aliases(
            display_name,
            normalized_targets,
            identity_index=identity_index,
            allow_direct_display_match=True,
        )
        if match is not None:
            stable_index = next(iter(stable_indices))
            candidate_plan[stable_index] = _SelectionPlanEntry(
                display_name=display_name,
                match=match,
            )
            for target_symbol in match.covered_targets:
                matches_by_target[target_symbol].append(stable_index)

    invalid_target_count = sum(
        1 for matches in matches_by_target.values() if len(matches) != 1
    )
    if invalid_target_count:
        raise ConversationSelectionError(
            f"{invalid_target_count} 个配置标识没有且仅有一个全局稳定会话，"
            "发送计划已整体取消"
        )

    # ``matches_by_target`` 已证明每个标识只落到一个索引。这里按索引去重后，同一
    # FriendIdentity 的多个别名自然合并为一次会话选择和一次 Enter。
    selected_indices = {
        matches[0] for matches in matches_by_target.values()
    }
    plan = {
        stable_index: candidate_plan[stable_index]
        for stable_index in selected_indices
    }
    return plan


def scroll_and_select_user(
    page: Any,
    username: str,
    targets: Sequence[str],
    identity_index: Optional[IdentityIndex] = None,
    friend_list_wait_time: Optional[int] = None,
    confirmation_timeout: Optional[int] = None,
) -> Iterable[ConfirmedConversation]:
    """先完整盘点好友列表，再依次产出经过双重 DOM 确认的目标会话。

    第一个点击发生前必须满足：每个可见项都有稳定 ``data-index``、只读扫描已经
    两轮稳定地证明到底、列表成功回顶、全部配置目标均能建立全局一对一计划。随后
    每次点击仍要求原项当前类名与右侧标题双重一致。任何证据失败都会停止账号。
    """

    if friend_list_wait_time is None:
        friend_list_wait_time = int(get_config()["friendListTimeout"])
    if confirmation_timeout is None:
        confirmation_timeout = int(get_config()["browserTimeout"])

    remaining_targets = [_normalize_identity_value(target) for target in targets]
    while remaining_targets:
        logger.debug("开始为剩余 %s 个配置标识只读预扫描全部好友会话", len(remaining_targets))
        inventory, list_handle = _scan_full_conversation_inventory(
            page,
            friend_list_wait_time,
        )
        plan = _build_unique_selection_plan(
            inventory,
            remaining_targets,
            identity_index,
        )
        logger.info(
            "发送计划已通过唯一性校验：配置标识数=%s，唯一会话数=%s，"
            "稳定库存会话数=%s",
            len(remaining_targets),
            len(plan),
            len(inventory),
        )

        selected: Optional[Tuple[_SelectionPlanEntry, Any]] = None
        for _ in range(MAX_INVENTORY_SCAN_ROUNDS):
            round_indices: Set[int] = set()
            for element in page.locator(CONVERSATION_ITEM_SELECTOR).all():
                stable_index, display_name = _read_conversation_item(element)
                if stable_index in round_indices:
                    raise ConversationSelectionError(
                        "发送阶段同一轮出现重复 data-index，列表状态已变化"
                    )
                round_indices.add(stable_index)
                if inventory.get(stable_index) != display_name:
                    raise ConversationSelectionError(
                        "发送阶段会话索引或标题偏离本轮预扫描库存，已在输入前终止"
                    )
                if stable_index not in plan:
                    continue

                plan_entry = plan[stable_index]
                if display_name != plan_entry.display_name:
                    raise ConversationSelectionError(
                        "目标会话标题偏离本轮预扫描计划，已在输入前终止"
                    )
                # response 回调可能在计划构建后继续补充同名身份。点击前必须基于
                # 当前集合重算一次；若唯一身份变为歧义或改为命中另一目标，旧计划
                # 立即失效，不能依赖几毫秒前的快照触碰会话。
                current_match = _match_target_aliases(
                    display_name,
                    remaining_targets,
                    identity_index=identity_index,
                    allow_direct_display_match=True,
                )
                if current_match != plan_entry.match:
                    raise ConversationSelectionError(
                        "点击前好友身份或别名覆盖集合已变化，发送计划已取消"
                    )
                selected = (plan_entry, element)
                break

            if selected is not None:
                break

            metrics = _read_list_metrics(page, list_handle)
            if _is_list_bottom(metrics):
                raise ConversationSelectionError(
                    "发送阶段扫描到底仍未出现计划目标，列表已偏离预扫描库存"
                )
            _scroll_list_forward(page, list_handle, metrics[1])
            _wait_for_page(page, friend_list_wait_time)
        else:
            raise ConversationSelectionError(
                "发送阶段超过安全扫描轮数，列表状态无法确认"
            )

        plan_entry, element = selected
        display_name = plan_entry.display_name

        # 逐项重验只能证明“当前待点击会话”仍覆盖原别名，却看不到另一个未选中的
        # 显示名是否刚被新到达的好友身份响应补成了同一别名。此时原会话的匹配值
        # 可以完全不变，但全局唯一性已经失效。因而在副作用边界前，必须用同一份
        # 只读 DOM 库存和当前最新身份索引完整重建计划；任何构建异常或计划差异都
        # 在 element.click() 之前终止，绝不把局部稳定误当成全局稳定。
        try:
            refreshed_plan = _build_unique_selection_plan(
                inventory,
                remaining_targets,
                identity_index,
            )
        except Exception as exc:
            raise ConversationSelectionError(
                "点击前完整发送计划无法重新通过唯一性校验，已取消点击"
            ) from exc
        if refreshed_plan != plan:
            raise ConversationSelectionError(
                "点击前完整发送计划已变化，已取消点击"
            )

        try:
            element.click()
            _wait_for_confirmed_conversation(
                page,
                element,
                display_name,
                confirmation_timeout,
            )
        except Exception as exc:
            if isinstance(exc, ConversationSelectionError):
                raise
            raise ConversationSelectionError(
                "点击目标会话或读取切换证据失败，已在输入前终止"
            ) from exc

        logger.debug("一个目标会话已通过全局唯一与双重 DOM 证据")
        yield ConfirmedConversation(
            target_symbol=plan_entry.match.covered_targets[0],
            display_name=display_name,
            item=element,
            covered_targets=plan_entry.match.covered_targets,
        )
        # 只有调用方完成唯一一次 Enter、编辑器清空验证并继续迭代时，才把本逻辑
        # 会话覆盖的全部配置别名一起移除。发送会让会话重排，所以剩余会话仍须
        # 重新完整盘点；data-index 从不跨发送复用，避免把新位置当作旧会话身份。
        for covered_target in plan_entry.match.covered_targets:
            remaining_targets.remove(covered_target)


def _split_message_lines(message: str) -> List[str]:
    """同时支持环境变量中的字面量 ``\\n`` 与真实换行符。"""

    normalized = message.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\\n", "\n").split("\n")


def _type_multiline_message(chat_input: Any, message: str) -> None:
    """逐行输入消息，并按位置而非文本值判断是否需要插入换行。

    旧实现拿当前行文本与最后一行比较；当较早的一行恰好和末行相同时，会错误地
    少插入一个换行。索引判断不会受重复文本影响。
    """

    lines = _split_message_lines(message)
    for index, line in enumerate(lines):
        if line:
            chat_input.type(line)
        if index < len(lines) - 1:
            chat_input.press("Shift+Enter")


def _read_slate_user_text(chat_input: Any) -> str:
    """只读取 Slate string 中的用户文本，排除 placeholder 与零宽占位内容。

    空编辑器也应至少存在一个 ``data-slate-leaf``。如果站点改版后该结构消失，
    返回猜测值可能把未知内容当作空草稿，因此这里直接抛出安全异常等待适配。
    真正的用户字符由 ``data-slate-string`` 标记；空编辑器只有 zero-width 节点，
    因而 string 列表为空是合法空值。读取后再次检查 leaf，避免 DOM 恰好在两次
    查询之间被移除时把空结果错误地当作编辑器清空。
    """

    leaves = chat_input.locator(SLATE_TEXT_LEAF_SELECTOR)
    if leaves.count() < 1:
        raise EditorSafetyError("Slate 文本叶节点不存在，无法安全判断草稿状态")
    strings = chat_input.locator(SLATE_TEXT_STRING_SELECTOR).all_inner_texts()
    if leaves.count() < 1:
        raise EditorSafetyError("读取草稿期间 Slate 结构已变化，无法确认编辑器为空")
    return _normalize_identity_value("\n".join(strings))


def _get_unique_empty_editor(page: Any, timeout_ms: int) -> Any:
    """取得唯一且无旧草稿的真实可编辑节点，否则在输入前终止。

    等待精确到 ``data-slate-editor`` 与 ``contenteditable=true`` 的节点，随后仍检查
    数量，避免 ``wait_for_selector`` 只返回首个元素而掩盖页面里并存的隐藏编辑器。
    草稿只从 Slate string 读取，显式排除 placeholder 和 zero-width 占位节点；任意
    实际字符都视为用户或上一次任务留下的草稿，自动化不得覆盖、拼接或发送。
    """

    page.wait_for_selector(CHAT_EDITOR_SELECTOR, timeout=timeout_ms)
    chat_input = page.locator(CHAT_EDITOR_SELECTOR)
    if chat_input.count() != 1:
        raise EditorSafetyError("可编辑消息节点不是唯一元素，已在输入前终止")
    if _read_slate_user_text(chat_input):
        raise EditorSafetyError("消息编辑器存在未发送旧草稿，已在输入前终止")
    return chat_input


def _wait_for_editor_cleared(
    page: Any,
    chat_input: Any,
    timeout_ms: int,
) -> None:
    """等待 Enter 后同一编辑器清空，超时则拒绝记录提交状态。

    这里只观察 UI 回执，不会重试 Enter。即使首次按键实际已被服务端接受，任何
    超时或 DOM 异常都保持失败语义，避免为追求成功率而产生重复发送副作用。
    """

    bounded_timeout = max(int(timeout_ms), 0)
    poll_interval = min(DOM_CONFIRM_POLL_INTERVAL_MS, max(bounded_timeout, 1))
    wait_count = (bounded_timeout + poll_interval - 1) // poll_interval

    for attempt in range(wait_count + 1):
        try:
            if chat_input.count() == 1 and not _read_slate_user_text(chat_input):
                return
        except Exception:
            # DOM 短暂重建不等于编辑器已经清空；只允许在时限内重新观察，不能把
            # Locator 失效当作提交证据。
            pass
        if attempt < wait_count:
            _wait_for_page(page, poll_interval)

    raise SubmissionConfirmationError(
        "Enter 后编辑器未在时限内清空，提交状态无法确认且不会重试 Enter"
    )


def _build_message() -> str:
    """延迟导入消息构建器，避免纯配置与选择器测试加载网络依赖。"""

    from core.msg_builder import build_message

    return build_message()


def _close_context(context: Any) -> None:
    """关闭账号上下文，且在已有主异常时不让清理异常覆盖根因。"""

    if context is None:
        return
    has_active_exception = sys.exc_info()[0] is not None
    try:
        context.close()
    except Exception:
        if has_active_exception:
            logger.exception("关闭账号浏览器上下文失败；保留原始任务异常")
            return
        raise


def do_user_task(
    browser: Any,
    username: str,
    cookies: Sequence[Mapping[str, Any]],
    targets: Sequence[str],
    runtime_config: Optional[Mapping[str, Any]] = None,
) -> TaskResult:
    """执行一个账号的同步任务，并保证其浏览器上下文最终被关闭。"""

    task_config = dict(runtime_config or get_config())
    context = None
    submitted_targets: List[str] = []
    requested_targets = tuple(targets)

    try:
        # 每个账号使用独立 context 和独立身份索引；Cookie、页面响应与好友映射
        # 均不会泄漏到其他账号。
        identity_index: Dict[str, Set[FriendIdentity]] = {}
        context = browser.new_context()
        configure_browser_context(context, task_config)

        page = context.new_page()
        response_handler = partial(
            handle_response,
            identity_index=identity_index,
            retries=task_config["taskRetryTimes"],
            retry_delay=task_config["friendListTimeout"] / 1000,
        )
        page.on("response", response_handler)
        context.add_cookies(list(cookies))

        retry_operation(
            "打开抖音网页聊天页面",
            page.goto,
            retries=task_config["taskRetryTimes"],
            delay=task_config["friendListTimeout"] / 1000,
            url="https://www.douyin.com/chat",
            wait_until="domcontentloaded",
        )

        # 先等待列表容器真实出现，再用 FRIEND_LIST_WAIT_TIME 留出初始数据和身份
        # 响应到达时间，替代旧版无条件等待 5 秒。
        page.wait_for_selector(
            CONVERSATION_LIST_SELECTOR,
            timeout=task_config["browserTimeout"],
        )
        _wait_for_page(page, task_config["friendListTimeout"])

        logger.debug("账号 %s 开始处理好友消息", username)
        message: Optional[str] = None
        for selection in scroll_and_select_user(
            page,
            username,
            targets,
            identity_index=identity_index,
            friend_list_wait_time=task_config["friendListTimeout"],
            confirmation_timeout=task_config["browserTimeout"],
        ):
            chat_input = _get_unique_empty_editor(
                page,
                task_config["browserTimeout"],
            )

            # 编辑器出现和草稿检查可能经历页面重渲染。输入前再次做一次无等待的
            # 双证据核验，确保当前项仍是刚才点击的同一会话。
            if not _conversation_selection_matches(
                page,
                selection.item,
                selection.display_name,
            ):
                raise ConversationSelectionError(
                    "输入前会话双重证据已失效，已终止当前账号"
                )

            # 同一账号同一轮只构建一次消息，避免模板含远程一言时对每个好友重复
            # 请求；如果一个目标都没找到，则完全不构建消息。远程一言请求可能
            # 阻塞，因此构建完成后不能直接沿用请求前的会话与空编辑器证据。
            if message is None:
                message = _build_message()

            # 消息构建期间页面可能收到新事件并切换或重建 DOM。再次取得唯一空
            # 编辑器并复核双重会话证据，确保从远程请求返回到首次 type 之间没有
            # 使用过期状态；任何旧草稿或标题变化都在输入动作之前终止。
            chat_input = _get_unique_empty_editor(
                page,
                task_config["browserTimeout"],
            )
            if not _conversation_selection_matches(
                page,
                selection.item,
                selection.display_name,
            ):
                raise ConversationSelectionError(
                    "消息构建后会话双重证据已失效，未执行输入"
                )
            _type_multiline_message(chat_input, message)

            # 输入过程也可能触发页面状态变化。在执行具有外部副作用且不可重试的
            # Enter 前最后核验一次会话，失配时宁可关闭上下文丢弃草稿也不冒险发送。
            if chat_input.count() != 1 or not _conversation_selection_matches(
                page,
                selection.item,
                selection.display_name,
            ):
                raise ConversationSelectionError(
                    "Enter 前会话或编辑器唯一性证据已失效，未执行发送按键"
                )

            logger.debug("账号 %s 已输入消息并完成 Enter 前安全核验", username)
            # Enter 是不可安全重试的副作用边界：此处只执行一次，之后只观察同一
            # 编辑器是否清空，不通过 retry_operation 包裹。
            chat_input.press("Enter")
            _wait_for_editor_cleared(
                page,
                chat_input,
                task_config["browserTimeout"],
            )
            # 一个已确认会话可能覆盖同一 FriendIdentity 的多个配置别名，但消息只
            # 按一次 Enter。清空证据成立后再把整组别名计入提交结果，防止别名组
            # 被误判为部分缺失并在后续循环对同一好友重复发送。
            submitted_targets.extend(selection.covered_targets)
            logger.info(
                "一个目标会话已确认且 Enter 后编辑器已清空：覆盖配置标识数=%s，"
                "记为已提交但未确认送达",
                len(selection.covered_targets),
            )

        missing_targets = tuple(
            target for target in requested_targets if target not in submitted_targets
        )
        if missing_targets:
            logger.error(
                "账号 %s 有 %s 个目标未找到或未能提交，账号任务记为失败",
                username,
                len(missing_targets),
            )
            return TaskResult(
                username=username,
                state=TaskState.PARTIAL_FAILURE,
                requested_targets=requested_targets,
                submitted_targets=tuple(submitted_targets),
                missing_targets=missing_targets,
            )

        return TaskResult(
            username=username,
            state=TaskState.SUBMITTED_UNCONFIRMED,
            requested_targets=requested_targets,
            submitted_targets=tuple(submitted_targets),
        )
    finally:
        _close_context(context)


def _close_browser_runtime(browser: Any, playwright: Any) -> List[BaseException]:
    """分别清理 browser 与 playwright，确保前者失败也不会跳过后者。"""

    errors: List[BaseException] = []
    if browser is not None:
        try:
            browser.close()
        except BaseException as exc:
            errors.append(exc)
            logger.exception("关闭浏览器实例失败")
    if playwright is not None:
        try:
            playwright.stop()
        except BaseException as exc:
            errors.append(exc)
            logger.exception("停止 Playwright 运行时失败")
    return errors


def runTasks(
    users: Optional[Sequence[Mapping[str, Any]]] = None,
    runtime_config: Optional[Mapping[str, Any]] = None,
    browser_factory: Optional[Callable[..., Tuple[Any, Any]]] = None,
) -> List[TaskResult]:
    """顺序执行全部账号；隔离账号失败，并在汇总失败时抛出非零语义异常。

    保留原有 ``runTasks`` 名称和无参数调用方式。账号之间仍顺序执行，既减少服务
    器峰值内存，也避免同一公网 IP 同时产生大量页面行为。单个账号失败会记录结果
    并继续下一个账号；全部清理完成后再统一抛出 ``TaskBatchError``。
    """

    global logger
    task_config = dict(runtime_config or get_config())
    task_users = list(users) if users is not None else list(get_user_data())
    logger = setup_logger(level=task_config.get("logLevel", "INFO"))

    factory = browser_factory or get_browser
    playwright = None
    browser = None
    results: List[TaskResult] = []
    cleanup_errors: List[BaseException] = []

    try:
        playwright, browser = factory(runtime_config=task_config)
        logger.info("开始执行 %s 个账号任务", len(task_users))

        for user in task_users:
            username = str(user.get("username", "未知用户"))
            targets = list(user.get("targets", []))
            logger.info("开始处理账号 %s", username)
            try:
                result = do_user_task(
                    browser,
                    username,
                    user.get("cookies", []),
                    targets,
                    runtime_config=task_config,
                )
            except Exception as exc:
                # 账号异常只记录脱敏后的账号名与异常，不输出 Cookie；随后继续处理
                # 下一个账号，实现真正的账号级失败隔离。
                logger.exception("账号 %s 执行失败，继续处理后续账号", username)
                result = TaskResult.failed(username, targets, exc)
            results.append(result)
    finally:
        cleanup_errors = _close_browser_runtime(browser, playwright)

    for cleanup_error in cleanup_errors:
        results.append(TaskResult.failed("浏览器运行时清理", (), cleanup_error))

    if any(not result.succeeded for result in results):
        raise TaskBatchError(results)

    logger.info("全部账号已完成提交；发送结果保持未确认状态")
    return results
