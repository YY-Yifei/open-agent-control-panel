"""Panel-facing error text: Chinese by default, English on request.

Why a separate catalog instead of inline strings: the panel UI is Chinese by
default (it toggles between 中文 and English), so the text a human sees when
something goes wrong has to follow the same switch. Keeping every message in one
place means a code, its status and both languages are written down once, and a
test can assert on the *code* instead of on wording that will keep changing.

Scope: this module covers ``/api/panel`` only — the panel's own surface. The ADP
contract (``/v1``) keeps its own error shape (``{code, message, detail}``) with
English messages, because those strings are part of a published spec and are
consumed by other implementations. The UI translates those codes client-side.

Wire shape of a panel error::

    {"detail": "<localized text>", "code": "<stable code>"}

``detail`` stays a plain string so old clients keep working; ``code`` is the
machine-readable half for anything that wants to branch without parsing prose.
"""

from __future__ import annotations

DEFAULT_LANG = "zh"
LANGS = ("zh", "en")

#: code -> {"zh": template, "en": template}. ``{name}`` placeholders are filled
#: from ``PanelError`` params. Both languages are mandatory: a half-translated
#: catalog is worse than none, so ``test_panel_errors.py`` checks the parity.
TEXTS: dict[str, dict[str, str]] = {
    "panel_token_required": {
        "zh": "需要面板口令（X-Panel-Token 请求头）",
        "en": "a panel token is required",
    },
    "agent_not_found": {
        "zh": "找不到 id 为 {agent_id} 的智能体",
        "en": "no agent with id {agent_id}",
    },
    "task_not_found": {
        "zh": "找不到 id 为 {task_id} 的任务",
        "en": "no task with id {task_id}",
    },
    "agent_gone": {
        "zh": "任务 {task_id} 所属的智能体已不在注册表中",
        "en": "the agent for task {task_id} is no longer registered",
    },
    "task_not_refreshable": {
        "zh": "该任务没有远端任务 id，无法刷新",
        "en": "this task has no remote task id to refresh",
    },
    "task_not_cancelable": {
        "zh": "该任务没有远端任务 id，无法取消",
        "en": "this task has no remote task id to cancel",
    },
    "bad_cursor": {
        "zh": "分页游标无效",
        "en": "cursor is not valid",
    },
    "cursor_mismatch": {
        "zh": "分页游标已不再对应任何记录",
        "en": "cursor no longer matches any entry",
    },
    # Removal. The panel can be asked to remove an agent in two ways: with the
    # manage token handed out at first registration (the contract's own rule),
    # or by the operator of a panel that is entitled to do it without one. The
    # first error below is what the UI shows when neither is available, and it
    # has to say *why* — "forbidden" would leave the user with no way forward.
    "manage_token_required": {
        "zh": "该智能体不是在本面板注册的，移除它需要首次注册时返回的 manage token；"
        "未配置面板口令，且当前不是本地模式（回环绑定），因此本面板无权直接移除",
        "en": "this agent was not registered through this panel, so removing it needs the "
        "manage token issued at first registration; no panel token is configured and this "
        "is not a loopback deployment, so the panel may not remove it by itself",
    },
    "manage_token_mismatch": {
        "zh": "manage token 不正确，只有首个注册者持有它",
        "en": "the manage token does not match; only the first registrant holds it",
    },
    # Conversations. The panel talks about sessions and messages; the words
    # "task" and "context" belong to the protocol and never reach these.
    "session_not_found": {
        "zh": "找不到 id 为 {session_id} 的会话",
        "en": "no conversation with id {session_id}",
    },
    "session_agent_gone": {
        "zh": "这段会话的智能体 {agent_id} 已不在面板中，无法继续发送",
        "en": "the agent {agent_id} for this conversation is no longer registered",
    },
    "empty_message": {
        "zh": "消息不能为空",
        "en": "a message cannot be empty",
    },
    "nothing_to_cancel": {
        "zh": "当前没有正在进行的回合可以停止",
        "en": "there is no running turn to stop",
    },
    "bad_title": {
        "zh": "会话标题不能为空",
        "en": "a conversation title cannot be empty",
    },
}


def _quality(params: str) -> float:
    """``;q=0.8`` -> ``0.8`` (1.0 when absent or unparsable)."""
    for param in params.split(";"):
        name, _, raw = param.partition("=")
        if name.strip().lower() in {"q", "quality"}:
            try:
                return float(raw.strip())
            except ValueError:
                return 0.0
    return 1.0


def normalize_lang(value: str | None) -> str:
    """Map an ``Accept-Language`` header onto ``zh`` or ``en``.

    The heaviest supported candidate wins (``en;q=0.4,zh;q=0.9`` is Chinese), and
    Chinese is both the default and the fallback for anything unrecognised: the
    panel ships Chinese-first, so a missing or exotic header must not silently
    turn the UI back into English.
    """
    if not value:
        return DEFAULT_LANG
    best: str | None = None
    best_q = 0.0
    for part in value.split(","):
        tag, _, params = part.strip().partition(";")
        tag = tag.strip().lower()
        candidate = "zh" if tag.startswith("zh") else "en" if tag.startswith("en") else None
        if candidate is None:
            continue
        quality = _quality(params)
        if quality > best_q:  # ties keep the earlier, more specific entry
            best, best_q = candidate, quality
    return best or DEFAULT_LANG


def text(code: str, lang: str = DEFAULT_LANG, **params: object) -> str:
    """Localized text for ``code``; never raises (an error path must not crash)."""
    entry = TEXTS.get(code)
    if entry is None:
        return code
    template = entry.get(lang) or entry[DEFAULT_LANG]
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        return template


class PanelError(Exception):
    """An error the panel reports to its own UI.

    Subclasses set ``status`` once; instances carry the code, its parameters and
    optional response headers, so the API layer can simply ``raise`` one and the
    handler in ``app.py`` turns it into a localized response.
    """

    status: int = 400

    def __init__(
        self,
        code: str,
        status: int | None = None,
        headers: dict[str, str] | None = None,
        **params: object,
    ) -> None:
        self.code = code
        self.params = params
        self.headers = headers or {}
        if status is not None:
            self.status = status
        super().__init__(text(code, "en", **params))

    def payload(self, lang: str = DEFAULT_LANG) -> dict[str, str]:
        """The JSON body: localized ``detail`` plus the stable ``code``."""
        return {"detail": text(self.code, lang, **self.params), "code": self.code}
