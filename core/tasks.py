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

import json
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
# 原子 DOM 快照没有副作用；页面恰在整棵虚拟列表提交时可能短暂返回空数组或协议
# 读取异常。最多三次、每次间隔 100ms 足以跨过一次常见重绘，又不会把永久页面
# 结构变化拖入后续 500 轮滚动协议或掩盖需要适配的新 DOM。
MAX_ATOMIC_DOM_SNAPSHOT_ATTEMPTS = 3
MAX_INVENTORY_SCAN_ROUNDS = 500
# 单次“从顶到底”扫描必须在底部连续取得三份完全相同的快照。两份快照只能证明
# 一个等待间隔内没有变化，线上懒加载曾在更晚的等待周期才继续扩展列表，因此这里
# 明确保留第三个观察点，避免暂时静止被误判为完整。
REQUIRED_STABLE_BOTTOM_SNAPSHOTS = 3
# 完整库存还要跨独立扫描复核。最多四次既允许首次扫描只拿到一部分懒加载数据，
# 又为持续抖动设置明确上限；若四次内始终没有相邻两份相同库存，就拒绝任何点击。
MAX_INVENTORY_SCAN_PASSES = 4
# 每次只前进三成视口，让相邻视口至少保留七成重叠。线上虚拟列表会在滚动时复用
# DOM；较大的重叠既提供“相邻窗口至少共享一个 data-index”的可验证证据，也仍由
# _scroll_list_forward 强制证明 scrollTop 实际前进，避免原地重复读取制造假稳定。
INVENTORY_SCROLL_STEP_RATIO = 0.3
# 到底后的轻微上下触碰用于重新触发依赖滚动事件的懒加载器。幅度刻意很小，不会
# 跳过任何项目；真正的完整性证据仍来自三份底部快照与跨 pass 的整表一致性。
INVENTORY_BOTTOM_NUDGE_RATIO = 0.1
# Enter 守卫只通过固定状态码与 Python 通信。状态中不包含会话 ID、标题或页面异常，
# 既便于在不可重试的按键边界做严格分支，也避免把权威证明泄漏到日志。
ENTER_AUTHORITY_GUARD_ARMED = "ENTER_AUTHORITY_GUARD_ARMED"
ENTER_AUTHORITY_GUARD_ALLOWED = "ENTER_AUTHORITY_GUARD_ALLOWED"
ENTER_AUTHORITY_GUARD_BLOCKED = "ENTER_AUTHORITY_GUARD_BLOCKED"
ENTER_AUTHORITY_GUARD_DISARMED = "ENTER_AUTHORITY_GUARD_DISARMED"


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
class _ConversationDomItemSnapshot:
    """一次浏览器事件循环内取得的会话项只读快照。

    ``stable_index``、``display_name`` 与 ``actionable`` 必须来自同一次
    ``Locator.evaluate_all``。它们不能再由 ``locator.all()`` 返回的动态 ``nth``
    Locator 分步读取，否则虚拟列表在两次 Playwright 往返之间重绘时，第二次读取
    可能已经指向另一项或零项。快照只授权后续继续核验，不直接授权点击；真正点击
    仍由权威会话边界在同一个 JavaScript 任务内重新检查全部证据。
    """

    stable_index: int
    display_name: str
    actionable: bool


@dataclass(frozen=True)
class ConversationAuthoritySnapshot:
    """IM SDK 与全局会话 store 在同一时刻给出的权威顺序快照。

    ``ordered_ids`` 是服务端会话身份，可能属于账号隐私，因此禁止进入 dataclass
    ``repr``。业务日志和异常只允许描述证据类型，不得包含列表内容。冻结对象保证
    一个已批准快照不会被 response 回调或测试替身就地修改；顺序参与相等比较，
    因而同一批 ID 的任何重排也会使旧发送计划立即失效。
    """

    has_more: bool
    sdk_is_loading: bool
    store_is_loading: bool
    ordered_ids: Tuple[str, ...] = field(repr=False)
    # participant sec_uid 与 ordered_ids 同位置绑定，是身份授权链的服务端端点；
    # 同属账号隐私，禁止出现在 repr 或异常文本中。
    participant_sec_user_ids: Tuple[str, ...] = field(repr=False)

    @property
    def is_terminal(self) -> bool:
        """只有分页结束且 SDK/store 均静止时才是可用于发送的终态。"""

        return (
            not self.has_more
            and not self.sdk_is_loading
            and not self.store_is_loading
        )


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
    # 旧版 do_user_task 单测会直接构造 selection，故两个新字段允许同时为 None。
    # 真实 scroll_and_select_user 产出的对象始终同时携带它们，并在点击后、输入前、
    # 消息构建后及 Enter 前持续复核。
    stable_index: Optional[int] = None
    authority_proof: Optional[ConversationAuthoritySnapshot] = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        """让旧的单目标构造方式自动得到一致的别名覆盖集合。"""

        if (self.stable_index is None) != (self.authority_proof is None):
            raise ValueError("稳定索引与权威快照必须同时提供或同时省略")
        if self.stable_index is not None and self.stable_index < 0:
            raise ValueError("稳定索引必须是非负整数")
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


def _preinstall_enter_capture_gate(context: Any) -> None:
    """在任何页面与站点脚本创建前预装最早的 Enter capture 门禁。

    后装 listener 即使使用 window capture，也会排在站点更早注册的同级监听器之后。
    ``BrowserContext.add_init_script`` 会在每个 document 的页面脚本之前执行，因此
    无修饰 Enter 一旦来自真实聊天编辑器，未 arm 或同步验证失败都会先被阻断。
    Shift+Enter 等带修饰组合不属于发送动作，必须继续放行给多行输入逻辑。门禁
    listener 随页面上下文销毁；每次发送只清理 arm/validator，不移除最早监听器。
    """

    editor_selector = json.dumps(CHAT_EDITOR_SELECTOR, ensure_ascii=False)
    init_script = f"""(() => {{
        /* preinstallEnterCaptureGate */
        const guardKey = "__DOUYIN_SPARK_FLOW_ENTER_GUARD_V1__";
        const editorSelector = {editor_selector};
        const disarmed = "ENTER_AUTHORITY_GUARD_DISARMED";
        const armed = "ENTER_AUTHORITY_GUARD_ARMED";
        const allowed = "ENTER_AUTHORITY_GUARD_ALLOWED";
        const blocked = "ENTER_AUTHORITY_GUARD_BLOCKED";
        if (Object.prototype.hasOwnProperty.call(window, guardKey)) return;

        let state = disarmed;
        let validator = null;
        let suppressRemainingEnterPhases = false;
        const rejectEvent = (event) => {{
            event.preventDefault();
            event.stopImmediatePropagation();
        }};
        const findTargetEditor = (event) => {{
            const target = event.target;
            if (!(target instanceof Element)) return null;
            if (target.matches(editorSelector)) return target;
            return target.closest(editorSelector);
        }};
        const isUnmodifiedEnterKeyEvent = (event) => (
            event.key === "Enter"
            && !event.shiftKey
            && !event.ctrlKey
            && !event.altKey
            && !event.metaKey
        );
        const onKeyDown = (event) => {{
            // 只接管真实“发送 Enter”。Shift+Enter 必须留给 Slate 插入换行，
            // 其他页面区域的 Enter 也不属于本任务的副作用边界。
            if (!isUnmodifiedEnterKeyEvent(event)) return;
            const targetEditor = findTargetEditor(event);
            if (targetEditor === null) return;
            // Chromium 即使 keydown 被 preventDefault，Playwright press 仍会继续
            // 派发 keyup。先记录本次发送序列，后续相位一律在 window capture 阻断，
            // 防止站点改用 keypress/keyup 重复或绕过 keydown 门禁提交。
            suppressRemainingEnterPhases = true;

            if (state !== armed || typeof validator !== "function") {{
                state = blocked;
                validator = null;
                rejectEvent(event);
                return;
            }}
            let proofStillValid = false;
            try {{
                // validator 是后续 arm 时捕获的纯同步函数。此处不得返回 Promise，
                // 从权威 proof 读取到事件继续传播之间不会产生微任务窗口。
                proofStillValid = validator(event, targetEditor) === true;
            }} catch (_ignored) {{
                proofStillValid = false;
            }}
            state = proofStillValid ? allowed : blocked;
            validator = null;
            if (!proofStillValid) rejectEvent(event);
        }};
        const onEnterKeyPhase = (event) => {{
            if (!isUnmodifiedEnterKeyEvent(event)) return;
            if (findTargetEditor(event) === null) return;
            rejectEvent(event);
            if (event.type === "keyup") suppressRemainingEnterPhases = false;
        }};
        const onBeforeInput = (event) => {{
            if (
                !suppressRemainingEnterPhases
                || (
                    event.inputType !== "insertParagraph"
                    && event.inputType !== "insertLineBreak"
                )
                || findTargetEditor(event) === null
            ) return;
            rejectEvent(event);
        }};
        const gate = Object.freeze({{
            version: 1,
            arm: (candidateValidator) => {{
                if (
                    state !== disarmed
                    || validator !== null
                    || typeof candidateValidator !== "function"
                ) return false;
                validator = candidateValidator;
                state = armed;
                return true;
            }},
            consume: () => {{
                const consumedStatus = state;
                validator = null;
                state = disarmed;
                suppressRemainingEnterPhases = false;
                return consumedStatus;
            }},
        }});
        Object.defineProperty(window, guardKey, {{
            value: gate,
            configurable: false,
            enumerable: false,
            writable: false,
        }});
        window.addEventListener("keydown", onKeyDown, true);
        window.addEventListener("keypress", onEnterKeyPhase, true);
        window.addEventListener("beforeinput", onBeforeInput, true);
        window.addEventListener("keyup", onEnterKeyPhase, true);
    }})();"""
    try:
        context.add_init_script(script=init_script)
    except Exception:
        raise ConversationSelectionError(
            "预装 Enter capture 门禁失败，已禁止创建账号页面"
        ) from None


def _read_authoritative_conversation_snapshot(
    page: Any,
) -> ConversationAuthoritySnapshot:
    """从页面唯一 IM remote 与全局 store 读取严格、可比较的终态快照。

    remote 名称带构建后缀，不能写死完整键名；但前缀必须严格匹配且候选只能有一个。
    SDK 插件容器可能是数组、Map 或普通对象，统一枚举其值后仍要求只有一个对象把
    ``initLinkInstance`` 声明为自身属性。页面脚本只返回最小结构，任何查找异常都
    转为 ``null``；Python 侧随后用固定文案 fail-closed，绝不把 ID、页面对象或
    Playwright 原始异常正文带入日志。
    """

    try:
        raw_snapshot = page.evaluate(
            """async () => {
                /* readAuthoritativeConversationSnapshot */
                try {
                    const listPluginValues = (plugins) => {
                        if (Array.isArray(plugins)) return Array.from(plugins);
                        if (plugins instanceof Map || plugins instanceof Set) {
                            return Array.from(plugins.values());
                        }
                        if (plugins && typeof plugins === "object") {
                            return Reflect.ownKeys(plugins).map((key) => plugins[key]);
                        }
                        return null;
                    };
                    const findUniqueLinkOwner = (sdk) => {
                        const values = listPluginValues(sdk && sdk.plugins);
                        if (values === null) return null;
                        const owners = values.filter((plugin) => (
                            plugin !== null
                            && (typeof plugin === "object" || typeof plugin === "function")
                            && Object.prototype.hasOwnProperty.call(
                                plugin,
                                "initLinkInstance",
                            )
                        ));
                        return owners.length === 1 ? owners[0] : null;
                    };
                    const remoteNames = Object.getOwnPropertyNames(window).filter(
                        (name) => name.startsWith("__VMOK_@pc-im/im:"),
                    );
                    if (remoteNames.length !== 1) return null;
                    // 线上 __VMOK__ 属性是 remote 容器，不是模块 exports。get('.')
                    // 异步返回同步 factory；await 完成后立即执行 factory，并在同一
                    // JavaScript 任务内读取其余引用和全部权威字段。
                    const remote = window[remoteNames[0]];
                    if (!remote || typeof remote.get !== "function") return null;
                    const factory = await remote.get(".");
                    if (typeof factory !== "function") return null;
                    const exportsObject = factory();
                    const context = exportsObject
                        && exportsObject.Context
                        && exportsObject.Context.instance;
                    const manager = context
                        && context.imSdkService
                        && context.imSdkService.imSdkManager;
                    if (!manager || typeof manager.getImSdkInstance !== "function") {
                        return null;
                    }
                    const sdk = manager.getImSdkInstance();
                    const linkOwner = findUniqueLinkOwner(sdk);
                    const link = linkOwner && linkOwner.initLinkInstance;
                    const store = window.conversationStore;
                    const nextParams = link && link.nextParams;
                    if (!link || !nextParams || !store) return null;
                    const orderedIds = store.sortedConversationIdList;
                    const conversationMap = store.conversationMap;
                    const participantSecUserIds = (
                        Array.isArray(orderedIds)
                        && conversationMap
                        && typeof conversationMap.get === "function"
                    ) ? orderedIds.map((conversationId) => {
                        const record = conversationMap.get(conversationId);
                        return record && record.toParticipantSecUserId;
                    }) : null;
                    return {
                        hasMore: nextParams.hasMore,
                        sdkIsLoading: link.isLoading,
                        storeIsLoading: store.isLoading,
                        orderedIds: Array.isArray(orderedIds)
                            ? Array.from(orderedIds)
                            : orderedIds,
                        participantSecUserIds,
                    };
                } catch (_ignored) {
                    return null;
                }
            }"""
        )
    except Exception:
        raise ConversationSelectionError(
            "读取 IM 会话权威状态失败，已禁止继续发送"
        ) from None

    if not isinstance(raw_snapshot, Mapping):
        raise ConversationSelectionError(
            "IM 会话权威状态结构缺失，已禁止继续发送"
        )

    has_more = raw_snapshot.get("hasMore")
    sdk_is_loading = raw_snapshot.get("sdkIsLoading")
    store_is_loading = raw_snapshot.get("storeIsLoading")
    ordered_ids = raw_snapshot.get("orderedIds")
    participant_sec_user_ids = raw_snapshot.get("participantSecUserIds")
    # 线上群组/系统会话的 ``toParticipantSecUserId`` 合法地返回空字符串。空槽仍是
    # ordered ID 同位置 proof 的一部分，原子点击和 Enter 必须逐项比较它；正式
    # 目标计划只会 join 非空 FriendIdentity.sec_uid，因此空槽不会获得发送授权。
    if (
        type(has_more) is not bool
        or type(sdk_is_loading) is not bool
        or type(store_is_loading) is not bool
        or not isinstance(ordered_ids, list)
        or not ordered_ids
        or any(
            not isinstance(conversation_id, str) or not conversation_id.strip()
            for conversation_id in ordered_ids
        )
        or len(set(ordered_ids)) != len(ordered_ids)
        or not isinstance(participant_sec_user_ids, list)
        or len(participant_sec_user_ids) != len(ordered_ids)
        or any(
            not isinstance(participant_id, str)
            for participant_id in participant_sec_user_ids
        )
    ):
        raise ConversationSelectionError(
            "IM 会话权威状态字段无效，已禁止继续发送"
        )

    return ConversationAuthoritySnapshot(
        has_more=has_more,
        sdk_is_loading=sdk_is_loading,
        store_is_loading=store_is_loading,
        ordered_ids=tuple(ordered_ids),
        participant_sec_user_ids=tuple(participant_sec_user_ids),
    )


def _read_atomic_conversation_dom_snapshot(
    page: Any,
) -> Tuple[_ConversationDomItemSnapshot, ...]:
    """原子读取当前 DOM 窗口里的索引、标题与可操作性。

    Playwright 的 ``locator.all()`` 只把当时的数量展开成一组仍会重新解析的 Locator，
    并不会冻结元素。抖音虚拟列表恰好会在网络响应、滚动和布局提交时复用这些节点；
    旧实现先 ``count()`` 再 ``get_attribute()``，两次协议往返之间因此存在明确的
    TOCTOU 窗口。这里让浏览器在一个同步 JavaScript 任务内遍历全部匹配元素，
    使最近 ``data-index`` 祖先、唯一标题和几何可见性属于同一时刻。

    该函数只返回经过严格形状校验的脱敏对象。页面脚本异常、列表容器不唯一、元素
    脱离、索引或标题结构异常都转换为固定 ``ConversationSelectionError``；调用方
    仍会在 authority 前后快照一致时才合并数据，绝不会因消除 100ms 超时而放宽
    完整库存或发送身份约束。
    """

    raw_snapshot: Any = None
    for attempt in range(MAX_ATOMIC_DOM_SNAPSHOT_ATTEMPTS):
        try:
            raw_snapshot = page.locator(CONVERSATION_ITEM_SELECTOR).evaluate_all(
                r"""(elements, selectors) => {
                /* readAtomicConversationDomSnapshot */
                const listContainers = document.querySelectorAll(
                    selectors.list,
                );
                const listContainer = listContainers.length === 1
                    ? listContainers[0]
                    : null;
                const items = elements.map((element) => {
                    // 从 parentElement 开始，保持原 ancestor::* 语义：列表项自身的
                    // 偶然同名属性不能替代虚拟列表容器提供的稳定位置证明。
                    const indexedAncestor = element.parentElement
                        && element.parentElement.closest("[data-index]");
                    const titles = element.querySelectorAll(selectors.title);
                    let actionable = false;
                    if (
                        element.isConnected
                        && listContainer
                        && listContainer.isConnected
                        && listContainer.contains(element)
                    ) {
                        const style = getComputedStyle(element);
                        const elementRect = element.getBoundingClientRect();
                        const listRect = listContainer.getBoundingClientRect();
                        actionable = (
                            style.display !== "none"
                            && style.visibility === "visible"
                            && style.pointerEvents !== "none"
                            && elementRect.width > 0
                            && elementRect.height > 0
                            && Math.min(elementRect.right, listRect.right)
                                > Math.max(elementRect.left, listRect.left)
                            && Math.min(elementRect.bottom, listRect.bottom)
                                > Math.max(elementRect.top, listRect.top)
                        );
                    }
                    return {
                        connected: element.isConnected,
                        stableIndex: indexedAncestor
                            ? indexedAncestor.getAttribute("data-index")
                            : null,
                        titleCount: titles.length,
                        displayName: titles.length === 1
                            ? titles[0].textContent
                            : null,
                        actionable,
                    };
                });
                return {
                    listContainerCount: listContainers.length,
                    items,
                };
                }""",
                {
                    "list": CONVERSATION_LIST_SELECTOR,
                    "title": CONVERSATION_TITLE_SELECTOR,
                },
            )
        except Exception:
            if attempt + 1 >= MAX_ATOMIC_DOM_SNAPSHOT_ATTEMPTS:
                raise ConversationSelectionError(
                    "原子读取会话 DOM 快照重试后仍失败，无法完成安全扫描"
                ) from None
            page.wait_for_timeout(DOM_CONFIRM_POLL_INTERVAL_MS)
            continue

        # 唯一列表容器中的非空 items 才算取得有效观察点。空数组可能来自 React
        # 正在替换虚拟列表根节点，允许短暂重读；非空但字段结构异常则在循环后立即
        # fail-closed，不能通过重试碰巧绕过永久错误项。
        if (
            isinstance(raw_snapshot, Mapping)
            and isinstance(raw_snapshot.get("items"), list)
            and raw_snapshot["items"]
        ):
            break
        if attempt + 1 < MAX_ATOMIC_DOM_SNAPSHOT_ATTEMPTS:
            page.wait_for_timeout(DOM_CONFIRM_POLL_INTERVAL_MS)

    if (
        not isinstance(raw_snapshot, Mapping)
        or type(raw_snapshot.get("listContainerCount")) is not int
        or raw_snapshot.get("listContainerCount") != 1
        or not isinstance(raw_snapshot.get("items"), list)
    ):
        raise ConversationSelectionError(
            "会话 DOM 快照中的列表容器或项目结构无效"
        )
    if not raw_snapshot["items"]:
        raise ConversationSelectionError(
            "原子会话 DOM 快照在有限重试后仍为空，无法证明当前可见窗口"
        )

    snapshots: List[_ConversationDomItemSnapshot] = []
    for raw_item in raw_snapshot["items"]:
        if not isinstance(raw_item, Mapping) or raw_item.get("connected") is not True:
            raise ConversationSelectionError(
                "会话项在原子快照期间已脱离 DOM，已取消本轮扫描"
            )
        raw_index = raw_item.get("stableIndex")
        if (
            not isinstance(raw_index, str)
            or re.fullmatch(r"0|[1-9]\d*", raw_index) is None
        ):
            raise ConversationSelectionError(
                "会话项缺少有效的稳定 data-index 祖先，无法完成安全扫描"
            )
        if (
            type(raw_item.get("titleCount")) is not int
            or raw_item.get("titleCount") != 1
        ):
            raise ConversationSelectionError(
                "会话项标题节点不是唯一元素，无法建立稳定身份映射"
            )
        display_name = _normalize_identity_value(raw_item.get("displayName"))
        if not display_name:
            raise ConversationSelectionError(
                "会话项标题为空，无法建立稳定索引与身份的映射"
            )
        actionable = raw_item.get("actionable")
        if type(actionable) is not bool:
            raise ConversationSelectionError(
                "会话项可操作性字段无效，无法继续安全扫描"
            )
        snapshots.append(
            _ConversationDomItemSnapshot(
                stable_index=int(raw_index),
                display_name=display_name,
                actionable=actionable,
            )
        )
    return tuple(snapshots)


def _conversation_item_selector_for_stable_index(stable_index: int) -> str:
    """构造由虚拟列表稳定索引锚定的会话项选择器。

    索引已经过非负整数校验，因此可以安全放入 CSS 属性选择器。与旧 ``nth``
    Locator 相比，该选择器在 DOM 节点重建后仍按本轮 authority 的位置重新解析；
    若页面意外产生零个或多个匹配，后续严格 ``Locator.evaluate`` 会 fail-closed。
    """

    if type(stable_index) is not int or stable_index < 0:
        raise ConversationSelectionError("会话稳定索引无效，无法构造安全定位器")
    return (
        f'{CONVERSATION_LIST_SELECTOR} '
        f'[data-index="{stable_index}"] {CONVERSATION_ITEM_SELECTOR}'
    )


def _click_conversation_at_authority_boundary(
    item: Any,
    stable_index: int,
    expected_display_name: str,
    authority_proof: ConversationAuthoritySnapshot,
) -> None:
    """在同一个浏览器任务内复核权威顺序、索引和连接状态后执行一次点击。

    若先在 Python 读取 authority、再调用 Playwright ``click``，两次协议往返之间页面
    仍可处理新的 IM 事件。这里改为 Locator.evaluate：浏览器在一个 JavaScript 任务
    内完成全部比较并调用 DOM ``element.click()``，从而把最后证据检查与首个副作用
    收敛到同一事件循环边界。异步 remote factory 在所有证明读取之前完成；从读取
    proof 到 ``element.click()`` 之间没有任何 await。返回值只有固定错误码，ID 既
    不写日志也不进入异常文本。
    """

    expected = {
        "stableIndex": stable_index,
        "displayName": expected_display_name,
        "hasMore": authority_proof.has_more,
        "sdkIsLoading": authority_proof.sdk_is_loading,
        "storeIsLoading": authority_proof.store_is_loading,
        "orderedIds": list(authority_proof.ordered_ids),
        "participantSecUserIds": list(authority_proof.participant_sec_user_ids),
    }
    try:
        clicked = item.evaluate(
            r"""async (element, expected) => {
                /* clickAtAuthoritativeConversationBoundary */
                const rejected = "AUTHORITY_BOUNDARY_REJECTED";
                try {
                    const listPluginValues = (plugins) => {
                        if (Array.isArray(plugins)) return Array.from(plugins);
                        if (plugins instanceof Map || plugins instanceof Set) {
                            return Array.from(plugins.values());
                        }
                        if (plugins && typeof plugins === "object") {
                            return Reflect.ownKeys(plugins).map((key) => plugins[key]);
                        }
                        return null;
                    };
                    const findUniqueLinkOwner = (sdk) => {
                        const values = listPluginValues(sdk && sdk.plugins);
                        if (values === null) return null;
                        const owners = values.filter((plugin) => (
                            plugin !== null
                            && (typeof plugin === "object" || typeof plugin === "function")
                            && Object.prototype.hasOwnProperty.call(
                                plugin,
                                "initLinkInstance",
                            )
                        ));
                        return owners.length === 1 ? owners[0] : null;
                    };
                    const normalizeTitle = (value) => String(value)
                        .normalize("NFKC")
                        .replace(/[\u200b\ufeff]/gu, "")
                        .replace(/\s+/gu, " ")
                        .trim();

                    const remoteNames = Object.getOwnPropertyNames(window).filter(
                        (name) => name.startsWith("__VMOK_@pc-im/im:"),
                    );
                    if (remoteNames.length !== 1) return rejected;
                    const remote = window[remoteNames[0]];
                    if (!remote || typeof remote.get !== "function") return rejected;
                    // 唯一 await 位于所有副作用边界证明之前。factory 返回后直到
                    // element.click() 不再让出事件循环。
                    const factory = await remote.get(".");
                    if (typeof factory !== "function") return rejected;
                    const exportsObject = factory();

                    if (
                        !element
                        || !element.isConnected
                        || element.ownerDocument !== document
                        || !(element instanceof HTMLElement)
                        || !element.matches(
                            ".conversationConversationItemwrapper",
                        )
                        || !Number.isSafeInteger(expected.stableIndex)
                        || expected.stableIndex < 0
                        || !Array.isArray(expected.orderedIds)
                        || !Array.isArray(expected.participantSecUserIds)
                        || expected.participantSecUserIds.length
                            !== expected.orderedIds.length
                        || expected.stableIndex >= expected.orderedIds.length
                    ) return rejected;
                    const indexedAncestor = element.closest("[data-index]");
                    const rawIndex = indexedAncestor
                        && indexedAncestor.getAttribute("data-index");
                    if (
                        !indexedAncestor
                        || !/^(0|[1-9]\d*)$/.test(rawIndex || "")
                        || rawIndex !== String(expected.stableIndex)
                    ) return rejected;

                    const titles = element.querySelectorAll(
                        ".conversationConversationItemtitle",
                    );
                    if (
                        titles.length !== 1
                        || normalizeTitle(titles[0].textContent || "")
                            !== expected.displayName
                    ) return rejected;

                    const listContainers = document.querySelectorAll(
                        ".conversationConversationListwrapper",
                    );
                    if (listContainers.length !== 1) return rejected;
                    const listContainer = listContainers[0];
                    if (
                        !listContainer.isConnected
                        || !listContainer.contains(element)
                    ) return rejected;
                    const style = getComputedStyle(element);
                    if (
                        style.display === "none"
                        || style.visibility !== "visible"
                        || style.pointerEvents === "none"
                    ) return rejected;
                    const elementRect = element.getBoundingClientRect();
                    const listRect = listContainer.getBoundingClientRect();
                    const intersects = (
                        elementRect.width > 0
                        && elementRect.height > 0
                        && Math.min(elementRect.right, listRect.right)
                            > Math.max(elementRect.left, listRect.left)
                        && Math.min(elementRect.bottom, listRect.bottom)
                            > Math.max(elementRect.top, listRect.top)
                    );
                    if (!intersects) return rejected;

                    const context = exportsObject
                        && exportsObject.Context
                        && exportsObject.Context.instance;
                    const manager = context
                        && context.imSdkService
                        && context.imSdkService.imSdkManager;
                    if (!manager || typeof manager.getImSdkInstance !== "function") {
                        return rejected;
                    }
                    const sdk = manager.getImSdkInstance();
                    const linkOwner = findUniqueLinkOwner(sdk);
                    const link = linkOwner && linkOwner.initLinkInstance;
                    const nextParams = link && link.nextParams;
                    const store = window.conversationStore;
                    const orderedIds = store && store.sortedConversationIdList;
                    const conversationMap = store && store.conversationMap;
                    if (
                        !nextParams
                        || !Array.isArray(orderedIds)
                        || !conversationMap
                        || typeof conversationMap.get !== "function"
                        || nextParams.hasMore !== expected.hasMore
                        || link.isLoading !== expected.sdkIsLoading
                        || store.isLoading !== expected.storeIsLoading
                        || orderedIds.length !== expected.orderedIds.length
                        || orderedIds.some(
                            (value, index) => value !== expected.orderedIds[index],
                        )
                        || orderedIds.some((conversationId, index) => {
                            const record = conversationMap.get(conversationId);
                            return (
                                !record
                                || record.toParticipantSecUserId
                                    !== expected.participantSecUserIds[index]
                            );
                        })
                        || orderedIds[expected.stableIndex]
                            !== expected.orderedIds[expected.stableIndex]
                    ) return rejected;

                    // 固定调用原生 HTMLElement click，避免页面在元素实例上覆盖同名
                    // 属性改变副作用；站点若拒绝非 trusted 事件，后续双 DOM 证据会
                    // fail-closed，绝不会直接进入编辑器输入。
                    HTMLElement.prototype.click.call(element);
                    return "AUTHORITY_BOUNDARY_CLICKED";
                } catch (_ignored) {
                    return "AUTHORITY_BOUNDARY_ERROR";
                }
            }""",
            expected,
        )
    except Exception:
        raise ConversationSelectionError(
            "权威会话点击边界执行失败，已在输入前终止"
        ) from None
    if clicked != "AUTHORITY_BOUNDARY_CLICKED":
        raise ConversationSelectionError(
            "点击边界的会话权威状态或稳定索引已变化，已取消点击"
        )


def _conversation_selection_matches(
    page: Any,
    item: Any,
    expected_display_name: str,
    probe_timeout_ms: int = DOM_CONFIRM_POLL_INTERVAL_MS,
    *,
    expected_stable_index: Optional[int] = None,
    expected_authority: Optional[ConversationAuthoritySnapshot] = None,
) -> bool:
    """一次性验证“原列表项选中 + 右侧标题一致”两项会话证据。

    标题比较统一经过 Python 侧 ``norm``，与好友列表和配置匹配规则完全一致，避免
    全角字符、不可见空格或连续空白让两个视觉上近似的名字被错误地视为同一人。
    右侧标题也必须唯一；零个或多个标题节点都属于页面状态不确定。
    """

    if (expected_stable_index is None) != (expected_authority is None):
        return False
    if expected_authority is not None:
        try:
            if (
                _read_authoritative_conversation_snapshot(page)
                != expected_authority
                or _read_stable_conversation_index(
                    item,
                    timeout_ms=probe_timeout_ms,
                )
                != expected_stable_index
            ):
                return False
        except Exception:
            # 这是只读布尔探针，调用方会转换为固定的阶段错误。不得把权威对象、
            # 会话 ID 或 Playwright 异常泄漏到选择确认日志。
            return False

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
    title_matches = (
        _normalize_identity_value(
            right_title.inner_text(timeout=bounded_probe_timeout)
        )
        == expected_display_name
    )
    if not title_matches:
        return False
    if expected_authority is not None:
        try:
            # class、右标题等 Locator 读取本身会泵送页面事件。全部 DOM 证据成立后
            # 再读一次 authority 和索引，防止顺序恰在第一次 proof 与标题之间变化，
            # 却被输入前或 Enter 前的布尔探针错误放行。
            return (
                _read_authoritative_conversation_snapshot(page)
                == expected_authority
                and _read_stable_conversation_index(
                    item,
                    timeout_ms=probe_timeout_ms,
                )
                == expected_stable_index
            )
        except Exception:
            return False
    return True


def _wait_for_confirmed_conversation(
    page: Any,
    item: Any,
    expected_display_name: str,
    timeout_ms: int,
    *,
    expected_stable_index: Optional[int] = None,
    expected_authority: Optional[ConversationAuthoritySnapshot] = None,
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
                expected_stable_index=expected_stable_index,
                expected_authority=expected_authority,
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
    """重叠地向前滚动约三成视口，并要求滚动位置确实推进。

    七成重叠让调用方能够验证相邻窗口确实共享 ``data-index``，避免虚拟列表恰好
    在边界替换 DOM 时漏项。步长仍以当前 ``clientHeight`` 动态计算，兼容服务器
    端不同窗口尺寸。
    """

    before, _, _ = _read_list_metrics(page, list_handle)
    step = max(client_height * INVENTORY_SCROLL_STEP_RATIO, 1)
    new_position = page.evaluate(
        """([element, step]) => {
            element.scrollTop = Math.min(
                element.scrollTop + step,
                Math.max(element.scrollHeight - element.clientHeight, 0),
            );
            return element.scrollTop;
        }""",
        [list_handle, step],
    )
    try:
        after = float(new_position)
    except (TypeError, ValueError) as exc:
        raise ConversationSelectionError("好友列表滚动结果异常，无法继续安全预扫描") from exc
    if after <= before:
        raise ConversationSelectionError(
            "好友列表尚未到底但滚动位置没有推进，预扫描无法证明完整"
        )


def _nudge_list_at_bottom(
    page: Any,
    list_handle: Any,
    client_height: float,
) -> None:
    """在底部做一次无点击、无输入的轻微滚动触碰。

    某些无限列表只在收到新的 scroll 事件后才检查是否继续拉取。脚本先向上移动
    一小段，在下一动画帧回到当时的真实底部；若等待期间列表扩展，外层下一轮会
    重新读取指标并继续向下扫描，而不会把旧 ``scrollHeight`` 当成完成证据。
    单页不可滚动列表无需触碰。
    """

    scroll_top, _, scroll_height = _read_list_metrics(page, list_handle)
    maximum_scroll_top = max(scroll_height - client_height, 0)
    if maximum_scroll_top <= 1:
        return

    raw_position = page.evaluate(
        """async ([element, distance]) => {
            const maximum = Math.max(
                element.scrollHeight - element.clientHeight,
                0,
            );
            element.scrollTop = Math.max(maximum - Math.min(distance, maximum), 0);
            await new Promise((resolve) => requestAnimationFrame(resolve));
            element.scrollTop = Math.max(
                element.scrollHeight - element.clientHeight,
                0,
            );
            return element.scrollTop;
        }""",
        [
            list_handle,
            max(client_height * INVENTORY_BOTTOM_NUDGE_RATIO, 1),
        ],
    )
    try:
        returned_position = float(raw_position)
    except (TypeError, ValueError) as exc:
        raise ConversationSelectionError(
            "好友列表底部触碰结果异常，无法继续验证懒加载稳定性"
        ) from exc
    if returned_position < 0 or returned_position + 1 < scroll_top:
        # 回到底部后的值不应明显小于触碰前位置；若页面脚本重写了滚动行为，继续
        # 扫描将失去“从顶到底”的可靠顺序，因此在任何点击前直接终止。
        raise ConversationSelectionError(
            "好友列表底部触碰后位置异常，无法证明预扫描完整"
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
    """用原子 DOM 快照记录当前窗口，并拒绝索引重复或跨轮映射冲突。"""

    round_indices: Set[int] = set()
    added_count = 0
    for item_snapshot in _read_atomic_conversation_dom_snapshot(page):
        stable_index = item_snapshot.stable_index
        display_name = item_snapshot.display_name
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


def _scan_conversation_inventory_pass(
    page: Any,
    list_handle: Any,
    friend_list_wait_time: int,
) -> Tuple[Dict[int, str], ConversationAuthoritySnapshot]:
    """执行一次绑定权威顺序的“回顶 -> 重叠滚动 -> 三快照到底”扫描。

    DOM ``data-index`` 只有在同一份 SDK/store 权威快照下才有可比较含义。每轮读取
    可见窗口前后都核对 authority；一旦变化，立即丢弃整个累计库存并验证回顶，
    绝不把旧顺序的索引与新顺序混在一张表里。相邻窗口还必须共享至少一个索引，
    将“70% 重叠”从滚动参数提升为可验收证据。

    到底后只有 authority 已终态、DOM 键精确覆盖 ``0..N-1``、物理位置确实在底部
    且三份连续签名完全一致才返回。``hasMore`` 或任一 loading 为真时，即使 DOM
    长时间保持 45 项也不会被当成完整列表。
    """

    _reset_list_to_top(page, list_handle, friend_list_wait_time)
    authority = _read_authoritative_conversation_snapshot(page)
    inventory: Dict[int, str] = {}
    previous_visible_indices: Optional[Set[int]] = None
    previous_bottom_signature: Optional[
        Tuple[
            Tuple[Tuple[int, str], ...],
            float,
            ConversationAuthoritySnapshot,
        ]
    ] = None
    stable_bottom_snapshots = 0

    for _ in range(MAX_INVENTORY_SCAN_ROUNDS):
        authority_before = _read_authoritative_conversation_snapshot(page)
        if authority_before != authority:
            # 顺序、分页或 loading 任一字段变化都会使已读 data-index 失去原语义。
            # 先清空再回顶，下一轮只在新 authority 下重新建立库存。
            authority = authority_before
            inventory.clear()
            previous_visible_indices = None
            previous_bottom_signature = None
            stable_bottom_snapshots = 0
            _reset_list_to_top(page, list_handle, friend_list_wait_time)
            continue

        if authority.sdk_is_loading or authority.store_is_loading:
            # loading 期间 DOM 可能处于中间提交状态，不能读取或滚动后保留任何项。
            # 等待页面事件推进；若状态改变，下一轮会走上面的回顶重扫分支。
            inventory.clear()
            previous_visible_indices = None
            previous_bottom_signature = None
            stable_bottom_snapshots = 0
            _wait_for_page(page, friend_list_wait_time)
            continue

        # 先把当前窗口读入临时映射；只有窗口读取后的 authority 仍完全相同，才会
        # 合并到本 pass 库存。这样 authority 在逐项读取期间变化也不会污染旧累计。
        visible_inventory: Dict[int, str] = {}
        _record_visible_inventory(page, visible_inventory)
        authority_after = _read_authoritative_conversation_snapshot(page)
        if authority_after != authority:
            authority = authority_after
            inventory.clear()
            previous_visible_indices = None
            previous_bottom_signature = None
            stable_bottom_snapshots = 0
            _reset_list_to_top(page, list_handle, friend_list_wait_time)
            continue

        visible_indices = set(visible_inventory)
        if not visible_indices:
            raise ConversationSelectionError(
                "可见会话窗口为空，无法证明权威库存与 DOM 一一覆盖"
            )
        if (
            previous_visible_indices is not None
            and not visible_indices.intersection(previous_visible_indices)
        ):
            raise ConversationSelectionError(
                "相邻会话窗口没有重叠索引，无法证明滚动扫描连续"
            )
        for stable_index, display_name in visible_inventory.items():
            previous_name = inventory.get(stable_index)
            if previous_name is not None and previous_name != display_name:
                raise ConversationSelectionError(
                    "同一权威顺序下 data-index 映射标题发生变化，已取消扫描"
                )
            inventory[stable_index] = display_name
        previous_visible_indices = visible_indices

        expected_indices = set(range(len(authority.ordered_ids)))
        if not set(inventory).issubset(expected_indices):
            raise ConversationSelectionError(
                "DOM 会话索引超出权威顺序范围，已禁止进入发送阶段"
            )

        metrics = _read_list_metrics(page, list_handle)
        if _is_list_bottom(metrics):
            inventory_complete = set(inventory) == expected_indices
            if authority.is_terminal and inventory_complete:
                signature = (
                    tuple(sorted(inventory.items())),
                    metrics[2],
                    authority,
                )
                if signature == previous_bottom_signature:
                    stable_bottom_snapshots += 1
                else:
                    previous_bottom_signature = signature
                    stable_bottom_snapshots = 1
                if stable_bottom_snapshots >= REQUIRED_STABLE_BOTTOM_SNAPSHOTS:
                    final_authority = _read_authoritative_conversation_snapshot(page)
                    if final_authority == authority:
                        return inventory, authority
                    # 第三份 DOM 快照之后 authority 才变化时也不能返回混合证据；
                    # 丢弃后回顶，让新顺序重新经历完整三快照协议。
                    authority = final_authority
                    inventory.clear()
                    previous_visible_indices = None
                    previous_bottom_signature = None
                    stable_bottom_snapshots = 0
                    _reset_list_to_top(page, list_handle, friend_list_wait_time)
                    continue
            else:
                previous_bottom_signature = None
                stable_bottom_snapshots = 0

            # 触碰本身没有业务副作用，只帮助触发依赖滚动事件的懒加载判断。随后
            # 完整等待一次；下一轮会重新读取可见项和滚动指标，列表若扩展就会把
            # 稳定计数清零并恢复向下滚动。
            _nudge_list_at_bottom(page, list_handle, metrics[1])
            _wait_for_page(page, friend_list_wait_time)
            continue

        previous_bottom_signature = None
        stable_bottom_snapshots = 0
        _scroll_list_forward(page, list_handle, metrics[1])
        _wait_for_page(page, friend_list_wait_time)

    raise ConversationSelectionError(
        "好友列表预扫描超过安全轮数，无法证明已经完整到达底部"
    )


def _scan_full_conversation_inventory(
    page: Any,
    friend_list_wait_time: int,
) -> Tuple[Dict[int, str], Any, ConversationAuthoritySnapshot]:
    """在任何点击前取得经两次独立全扫描确认的稳定库存。

    单个 pass 即使在底部连续稳定三轮，也可能只看见服务端暂时返回的前一批会话。
    因此本函数强制重新回顶并再走完整列表，只有相邻两个 pass 的 ``data-index ->
    标题`` 映射逐项相等才允许回顶并交给发送计划。首次不一致可继续，最多四个
    pass；持续变化说明完整性无法证明，必须 fail-closed。
    """

    list_handle = _get_conversation_list_handle(page)
    previous_inventory: Optional[Dict[int, str]] = None
    previous_authority: Optional[ConversationAuthoritySnapshot] = None

    for _ in range(MAX_INVENTORY_SCAN_PASSES):
        current_inventory, current_authority = _scan_conversation_inventory_pass(
            page,
            list_handle,
            friend_list_wait_time,
        )
        if (
            previous_inventory is not None
            and current_inventory == previous_inventory
            and current_authority == previous_authority
        ):
            # 返回前再次验证顶部，既满足首个点击从已知起点开始，也防止第二个 pass
            # 到底后页面自行反弹却被调用方误认为仍可按库存索引查找。
            _reset_list_to_top(page, list_handle, friend_list_wait_time)
            final_authority = _read_authoritative_conversation_snapshot(page)
            if final_authority != current_authority:
                raise ConversationSelectionError(
                    "完整扫描后回顶时权威会话顺序已变化，已取消发送计划"
                )
            return current_inventory, list_handle, current_authority
        previous_inventory = dict(current_inventory)
        previous_authority = current_authority

    raise ConversationSelectionError(
        "好友列表连续完整扫描始终不一致，无法证明库存稳定，已禁止进入发送阶段"
    )


def _build_unique_selection_plan(
    inventory: Mapping[int, str],
    targets: Sequence[str],
    identity_index: Optional[IdentityIndex],
    authority: Optional[ConversationAuthoritySnapshot] = None,
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

    if authority is not None and identity_index is not None:
        # 正式路径不再从 DOM 标题反推身份。user/info 的冻结 FriendIdentity.sec_uid
        # 必须与 authority 同索引 participant 一一 join；昵称/备注只可作为同一身份
        # 的冗余别名，每个逻辑会话至少包含一个短号、抖音号或 sec_uid 强锚点。
        identities = {
            identity
            for identity_set in identity_index.values()
            for identity in identity_set
        }
        matches_by_target: Dict[str, List[int]] = {
            target: [] for target in normalized_targets
        }
        candidate_plan: Dict[int, _SelectionPlanEntry] = {}
        normalized_participants = tuple(
            _normalize_identity_value(participant_id)
            for participant_id in authority.participant_sec_user_ids
        )
        for identity in identities:
            if not identity.sec_uid:
                continue
            participant_indices = [
                index
                for index, participant_id in enumerate(normalized_participants)
                if participant_id == identity.sec_uid
            ]
            if len(participant_indices) != 1:
                continue
            all_candidates = set(identity.candidates())
            strong_candidates = {
                candidate
                for candidate in (
                    identity.short_id,
                    identity.unique_id,
                    identity.sec_uid,
                )
                if candidate
            }
            covered_targets = tuple(
                target for target in normalized_targets if target in all_candidates
            )
            if (
                not covered_targets
                or not any(target in strong_candidates for target in covered_targets)
            ):
                continue
            stable_index = participant_indices[0]
            if stable_index not in inventory or stable_index in candidate_plan:
                raise ConversationSelectionError(
                    "participant 身份与稳定会话索引不是一一映射，发送计划已取消"
                )
            match = _TargetAliasMatch(covered_targets, identity)
            candidate_plan[stable_index] = _SelectionPlanEntry(
                display_name=inventory[stable_index],
                match=match,
            )
            for target_symbol in covered_targets:
                matches_by_target[target_symbol].append(stable_index)

        invalid_target_count = sum(
            1 for matches in matches_by_target.values() if len(matches) != 1
        )
        if invalid_target_count:
            raise ConversationSelectionError(
                f"{invalid_target_count} 个配置标识未由唯一 participant 强身份覆盖，"
                "发送计划已整体取消"
            )
        selected_indices = {matches[0] for matches in matches_by_target.values()}
        return {
            stable_index: candidate_plan[stable_index]
            for stable_index in selected_indices
        }

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
        inventory, list_handle, authority_proof = _scan_full_conversation_inventory(
            page,
            friend_list_wait_time,
        )
        plan = _build_unique_selection_plan(
            inventory,
            remaining_targets,
            identity_index,
            authority=authority_proof,
        )
        logger.info(
            "发送计划已通过唯一性校验：配置标识数=%s，唯一会话数=%s，"
            "稳定库存会话数=%s",
            len(remaining_targets),
            len(plan),
            len(inventory),
        )

        selected: Optional[Tuple[int, _SelectionPlanEntry, Any]] = None
        for _ in range(MAX_INVENTORY_SCAN_ROUNDS):
            authority_before = _read_authoritative_conversation_snapshot(page)
            if authority_before != authority_proof:
                raise ConversationSelectionError(
                    "发送阶段读取窗口前权威会话顺序已变化，已取消点击"
                )
            round_indices: Set[int] = set()
            for item_snapshot in _read_atomic_conversation_dom_snapshot(page):
                stable_index = item_snapshot.stable_index
                display_name = item_snapshot.display_name
                if stable_index in round_indices:
                    raise ConversationSelectionError(
                        "发送阶段同一轮出现重复 data-index，列表状态已变化"
                    )
                round_indices.add(stable_index)
                if inventory.get(stable_index) != display_name:
                    raise ConversationSelectionError(
                        "发送阶段会话索引或标题偏离本轮预扫描库存，已在输入前终止"
                    )
                if stable_index not in plan or selected is not None:
                    continue

                plan_entry = plan[stable_index]
                if display_name != plan_entry.display_name:
                    raise ConversationSelectionError(
                        "目标会话标题偏离本轮预扫描计划，已在输入前终止"
                    )
                if not item_snapshot.actionable:
                    # 虚拟列表的 overscan 会把尚未进入容器可视区域的目标保留在
                    # DOM。可操作性与索引、标题来自同一次浏览器快照，避免另一次
                    # Locator.evaluate 恰遇重绘；真正点击边界仍会原子复核几何。
                    continue
                element = page.locator(
                    _conversation_item_selector_for_stable_index(stable_index)
                )
                selected = (stable_index, plan_entry, element)

            authority_after = _read_authoritative_conversation_snapshot(page)
            if authority_after != authority_proof:
                raise ConversationSelectionError(
                    "发送阶段读取窗口后权威会话顺序已变化，已取消点击"
                )

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

        stable_index, plan_entry, element = selected
        display_name = plan_entry.display_name

        # 身份授权已冻结在 plan_entry.identity 与 authority participant proof 中。
        # response 回调继续修改 identity_index 不能改变本轮许可；点击边界只重验
        # 不可变 authority 与 DOM index/title，避免 mutable 索引撤销或替换授权。

        try:
            if _read_authoritative_conversation_snapshot(page) != authority_proof:
                raise ConversationSelectionError(
                    "点击前权威会话顺序已变化，已取消点击"
                )
            _click_conversation_at_authority_boundary(
                element,
                stable_index,
                display_name,
                authority_proof,
            )
            _wait_for_confirmed_conversation(
                page,
                element,
                display_name,
                confirmation_timeout,
                expected_stable_index=stable_index,
                expected_authority=authority_proof,
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
            stable_index=stable_index,
            authority_proof=authority_proof,
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


def _install_enter_authority_guard(
    chat_input: Any,
    selection: ConfirmedConversation,
) -> None:
    """解析权威引用，并把一次性同步 validator 原子 arm 到预装门禁。

    Python 的最后一次 proof 读取与 Playwright ``press`` 之间仍有协议往返，IM 顺序
    可能恰在这个窗口变化。最早 window capture listener 已由 context init script
    预装；本函数只异步解析 remote factory，捕获严格校验过的 manager/SDK/link/store
    引用，随后在同一个 JavaScript 任务内把纯同步 validator arm 到既有门禁。真实
    keydown 到达时不再 await，并复核完整 authority、active 会话与同一编辑器。
    """

    if (
        type(selection.stable_index) is not int
        or selection.stable_index < 0
        or not isinstance(
            selection.authority_proof,
            ConversationAuthoritySnapshot,
        )
        or not selection.authority_proof.is_terminal
    ):
        raise ConversationSelectionError(
            "Enter 守卫缺少稳定索引或权威快照，已禁止发送"
        )
    proof = selection.authority_proof
    expected = {
        "stableIndex": selection.stable_index,
        "displayName": selection.display_name,
        "hasMore": proof.has_more,
        "sdkIsLoading": proof.sdk_is_loading,
        "storeIsLoading": proof.store_is_loading,
        "orderedIds": list(proof.ordered_ids),
        "participantSecUserIds": list(proof.participant_sec_user_ids),
        "editorSelector": CHAT_EDITOR_SELECTOR,
        "itemSelector": CONVERSATION_ITEM_SELECTOR,
        "itemTitleSelector": CONVERSATION_TITLE_SELECTOR,
        "currentClass": CURRENT_CONVERSATION_CLASS,
        "rightTitleSelector": RIGHT_PANEL_TITLE_SELECTOR,
    }
    try:
        status = chat_input.evaluate(
            r"""async (editor, expected) => {
                /* installEnterAuthorityGuard */
                const guardKey = "__DOUYIN_SPARK_FLOW_ENTER_GUARD_V1__";
                const armed = "ENTER_AUTHORITY_GUARD_ARMED";
                const setupError = "ENTER_AUTHORITY_GUARD_SETUP_ERROR";
                try {
                    const gate = window[guardKey];
                    if (
                        !editor
                        || !editor.isConnected
                        || !gate
                        || gate.version !== 1
                        || typeof gate.arm !== "function"
                        || !Number.isSafeInteger(expected.stableIndex)
                        || expected.stableIndex < 0
                        || !Array.isArray(expected.orderedIds)
                        || expected.orderedIds.length === 0
                        || !Array.isArray(expected.participantSecUserIds)
                        || expected.participantSecUserIds.length
                            !== expected.orderedIds.length
                        || expected.stableIndex >= expected.orderedIds.length
                    ) return setupError;

                    const listPluginValues = (plugins) => {
                        if (Array.isArray(plugins)) return Array.from(plugins);
                        if (plugins instanceof Map || plugins instanceof Set) {
                            return Array.from(plugins.values());
                        }
                        if (plugins && typeof plugins === "object") {
                            return Reflect.ownKeys(plugins).map((key) => plugins[key]);
                        }
                        return null;
                    };
                    const findUniqueLinkOwner = (sdkValue) => {
                        const values = listPluginValues(sdkValue && sdkValue.plugins);
                        if (values === null) return null;
                        const owners = values.filter((plugin) => (
                            plugin !== null
                            && (typeof plugin === "object" || typeof plugin === "function")
                            && Object.prototype.hasOwnProperty.call(
                                plugin,
                                "initLinkInstance",
                            )
                        ));
                        return owners.length === 1 ? owners[0] : null;
                    };
                    const normalizeTitle = (value) => String(value)
                        .normalize("NFKC")
                        .replace(/[\u200b\ufeff]/gu, "")
                        .replace(/\s+/gu, " ")
                        .trim();

                    const remoteNames = Object.getOwnPropertyNames(window).filter(
                        (name) => name.startsWith("__VMOK_@pc-im/im:"),
                    );
                    if (remoteNames.length !== 1) return setupError;
                    const remoteName = remoteNames[0];
                    const remote = window[remoteName];
                    if (!remote || typeof remote.get !== "function") return setupError;
                    // remote.get 是守卫安装阶段唯一允许的 await。它发生在监听器
                    // 生效之前；keydown 的证明读取与站点 Enter 处理之间绝不让出
                    // JavaScript 事件循环。
                    const factory = await remote.get(".");
                    if (typeof factory !== "function") return setupError;
                    const exportsObject = factory();
                    const context = exportsObject
                        && exportsObject.Context
                        && exportsObject.Context.instance;
                    const manager = context
                        && context.imSdkService
                        && context.imSdkService.imSdkManager;
                    if (!manager || typeof manager.getImSdkInstance !== "function") {
                        return setupError;
                    }
                    const sdk = manager.getImSdkInstance();
                    const linkOwner = findUniqueLinkOwner(sdk);
                    const link = linkOwner && linkOwner.initLinkInstance;
                    const store = window.conversationStore;
                    if (!link || !store) return setupError;

                    const authorityMatches = () => {
                        const currentRemoteNames = Object.getOwnPropertyNames(window).filter(
                            (name) => name.startsWith("__VMOK_@pc-im/im:"),
                        );
                        if (
                            currentRemoteNames.length !== 1
                            || currentRemoteNames[0] !== remoteName
                            || window[remoteName] !== remote
                            || window.conversationStore !== store
                            || manager.getImSdkInstance() !== sdk
                        ) return false;
                        const currentLinkOwner = findUniqueLinkOwner(sdk);
                        if (
                            !currentLinkOwner
                            || currentLinkOwner.initLinkInstance !== link
                        ) return false;
                        const nextParams = link.nextParams;
                        const orderedIds = store.sortedConversationIdList;
                        const conversationMap = store.conversationMap;
                        return (
                            nextParams
                            && Array.isArray(orderedIds)
                            && conversationMap
                            && typeof conversationMap.get === "function"
                            && nextParams.hasMore === expected.hasMore
                            && link.isLoading === expected.sdkIsLoading
                            && store.isLoading === expected.storeIsLoading
                            && orderedIds.length === expected.orderedIds.length
                            && orderedIds.every(
                                (value, index) => value === expected.orderedIds[index],
                            )
                            && orderedIds.every((conversationId, index) => {
                                const record = conversationMap.get(conversationId);
                                return (
                                    record
                                    && record.toParticipantSecUserId
                                        === expected.participantSecUserIds[index]
                                );
                            })
                        );
                    };

                    const domAndEventMatch = (event, gateEditor) => {
                        if (
                            event.defaultPrevented
                            || !event.isTrusted
                            || event.shiftKey
                            || event.ctrlKey
                            || event.altKey
                            || event.metaKey
                            || event.repeat
                            || event.isComposing
                        ) return false;
                        const editors = document.querySelectorAll(
                            expected.editorSelector,
                        );
                        if (
                            editors.length !== 1
                            || editors[0] !== editor
                            || gateEditor !== editor
                            || !editor.isConnected
                        ) return false;
                        const eventTarget = event.target;
                        const focused = document.activeElement;
                        if (
                            !eventTarget
                            || !(
                                eventTarget === editor
                                || editor.contains(eventTarget)
                            )
                            || !focused
                            || !(
                                focused === editor
                                || editor.contains(focused)
                            )
                        ) return false;

                        const activeItems = document.querySelectorAll(
                            `${expected.itemSelector}.${expected.currentClass}`,
                        );
                        if (activeItems.length !== 1) return false;
                        const activeItem = activeItems[0];
                        if (
                            !activeItem.isConnected
                            || !activeItem.classList.contains(expected.currentClass)
                        ) return false;
                        const indexedAncestor = activeItem.closest("[data-index]");
                        const rawIndex = indexedAncestor
                            && indexedAncestor.getAttribute("data-index");
                        if (
                            !indexedAncestor
                            || !/^(0|[1-9]\d*)$/.test(rawIndex || "")
                            || rawIndex !== String(expected.stableIndex)
                        ) return false;
                        const itemTitles = activeItem.querySelectorAll(
                            expected.itemTitleSelector,
                        );
                        const rightTitles = document.querySelectorAll(
                            expected.rightTitleSelector,
                        );
                        return (
                            itemTitles.length === 1
                            && rightTitles.length === 1
                            && normalizeTitle(itemTitles[0].textContent || "")
                                === expected.displayName
                            && normalizeTitle(rightTitles[0].textContent || "")
                                === expected.displayName
                        );
                    };

                    // arm 前先证明捕获到的引用与 Python proof 完全一致。之后到
                    // gate.arm 没有 await，页面不能在中间提交另一个状态；真正
                    // keydown 会由 init script 预装的最早 listener 调用此 validator。
                    if (!authorityMatches()) return setupError;
                    const validator = (event, gateEditor) => {
                        let proofStillValid = false;
                        try {
                            proofStillValid = (
                                authorityMatches()
                                && domAndEventMatch(event, gateEditor)
                            );
                        } catch (_ignored) {
                            proofStillValid = false;
                        }
                        return proofStillValid;
                    };
                    return gate.arm(validator) === true ? armed : setupError;
                } catch (_ignored) {
                    return setupError;
                }
            }""",
            expected,
        )
    except Exception:
        raise ConversationSelectionError(
            "安装 Enter 权威守卫失败，已禁止发送"
        ) from None
    if status != ENTER_AUTHORITY_GUARD_ARMED:
        raise ConversationSelectionError(
            "Enter 权威守卫未能安全就绪，已禁止发送"
        )


def _consume_enter_authority_guard_status(page: Any) -> str:
    """读取一次守卫状态并清空 arm；预装 capture listener 保留到 context 销毁。"""

    try:
        status = page.evaluate(
            """() => {
                /* consumeEnterAuthorityGuardStatus */
                const guardKey = "__DOUYIN_SPARK_FLOW_ENTER_GUARD_V1__";
                const guard = window[guardKey];
                try {
                    if (!guard || typeof guard.consume !== "function") {
                        return "ENTER_AUTHORITY_GUARD_STATUS_MISSING";
                    }
                    // consume 在页面闭包内先取本次 keydown 结果，再同步清空 validator
                    // 和 arm 状态。最早注册的 listener 不移除，下一次发送仍先于站点。
                    const currentStatus = guard.consume();
                    return typeof currentStatus === "string"
                        ? currentStatus
                        : "ENTER_AUTHORITY_GUARD_STATUS_ERROR";
                } catch (_ignored) {
                    return "ENTER_AUTHORITY_GUARD_STATUS_ERROR";
                }
            }"""
        )
    except Exception:
        raise ConversationSelectionError(
            "读取并清理 Enter 权威守卫失败，提交状态不可信"
        ) from None
    return status if isinstance(status, str) else "ENTER_AUTHORITY_GUARD_STATUS_ERROR"


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
        # 必须先注册 context init script，再创建首个 page；否则站点可能已经在
        # window capture 上抢先注册 Enter 发送处理器，晚装门禁无法保证先阻断。
        _preinstall_enter_capture_gate(context)

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
            # ConfirmedConversation 保留两个 Optional 仅用于旧调用方构造兼容；真实
            # 发送路径绝不能因此退回 DOM-only。必须在取得编辑器、构建消息或输入
            # 任何字符之前证明生成器携带了稳定索引与完整 authority 快照。
            if (
                type(selection.stable_index) is not int
                or selection.stable_index < 0
                or not isinstance(
                    selection.authority_proof,
                    ConversationAuthoritySnapshot,
                )
                or not selection.authority_proof.is_terminal
                or selection.stable_index
                >= len(selection.authority_proof.ordered_ids)
            ):
                raise ConversationSelectionError(
                    "已确认会话缺少稳定索引或权威快照，已在编辑器操作前终止"
                )
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
                expected_stable_index=selection.stable_index,
                expected_authority=selection.authority_proof,
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
                expected_stable_index=selection.stable_index,
                expected_authority=selection.authority_proof,
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
                expected_stable_index=selection.stable_index,
                expected_authority=selection.authority_proof,
            ):
                raise ConversationSelectionError(
                    "Enter 前会话或编辑器唯一性证据已失效，未执行发送按键"
                )

            logger.debug("账号 %s 已输入消息并完成 Enter 前安全核验", username)
            # Python proof 与 press 之间仍有浏览器协议窗口，因此先在页面 window
            # capture 安装一次性守卫。真实 keydown 内会同步复核完整 authority、
            # active 会话与同一编辑器；失配时事件在站点处理器之前即被阻断。
            _install_enter_authority_guard(chat_input, selection)
            try:
                # Enter 是不可安全重试的副作用边界：无论 Playwright 返回、抛错或
                # 页面守卫拒绝，本函数都只调用一次 press。
                chat_input.press("Enter")
            except BaseException:
                try:
                    _consume_enter_authority_guard_status(page)
                except Exception:
                    # 清理失败不能覆盖原始按键异常；账号上下文仍会关闭，且绝不
                    # 据此重试可能已经到达页面的 Enter。
                    pass
                raise
            guard_status = _consume_enter_authority_guard_status(page)
            if guard_status != ENTER_AUTHORITY_GUARD_ALLOWED:
                raise ConversationSelectionError(
                    "Enter 到达页面时权威会话或编辑器证据已变化，按键已被守卫阻止"
                )

            # 只有 capture guard 明确记录 allowed 后才观察同一编辑器清空；armed、
            # blocked、missing 或任何固定错误状态都不会进入提交确认阶段。
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
