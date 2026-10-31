"""iinfo 调试插件：发送 iinfo，返回当前会话能拿到的全部信息。

只用 NoneBot 核心抽象（Bot/Event 基类方法 + 事件模型自带的全量字段 dump），
不依赖任何第三方插件；适配器特有字段通过 getattr 探测，探测到才显示，
因此在任何适配器下都能直接加载使用。

针对 QQ 官方 Bot（openid 体系）做了增强：
- 精简版（默认）单条回复给出会话全貌，避开 C2C 被动消息最多回 4 次的限制
- ids 专版：汇总全部标识符（openid / guild / 消息 ID / msg_idx），供配置迁移使用
- msg 专版：消息结构（段、附件、提及、引用、message_scene）
- api 专版：探测当前协议端可用的 API（me / get_group_info / get_guild / get_login_info）
- full（完整）版：完整摘要 + 分段事件 JSON
"""

from __future__ import annotations

import json
import re
from typing import Any

from nonebot import on_command
from nonebot.adapters import Bot, Event
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="iinfo",
    description="发送 iinfo 查看当前会话信息；iinfo ids/msg/api/full 查看标识符/消息结构/API 探测/完整版",
    usage=(
        "iinfo —— 精简版（默认，单条回复）\n"
        "iinfo ids —— 标识符专版（openid 汇总，配置迁移用）\n"
        "iinfo msg —— 消息结构专版\n"
        "iinfo api —— API 探测专版\n"
        "iinfo full / iinfo完整 —— 完整版（含分段 JSON）"
    ),
    type="application",
)

iinfo = on_command("iinfo", priority=5, block=True)

# 单条消息的安全长度：JSON 超出后分段发送，避免触发平台的消息长度限制
_CHUNK_SIZE = 3000
# 精简版纯文本预览的最大长度
_PREVIEW_LEN = 60

# 各适配器常见的事件特有字段，存在才显示（完整版用）
_PROBE_EVENT_FIELDS = (
    "group_id",
    "group_openid",
    "guild_id",
    "channel_id",
    "message_id",
    "sub_type",
    "font",
    "time",
    "self_id",
)
_PROBE_SENDER_FIELDS = (
    "nickname",
    "card",
    "sex",
    "age",
    "area",
    "level",
    "title",
    "role",
)
# QQ 官方适配器（openid 体系）author 字段，存在才显示
_PROBE_QQ_AUTHOR_FIELDS = (
    "id",
    "username",
    "member_openid",
    "user_openid",
    "union_openid",
    "member_role",
    "bot",
)


# ============ 通用小工具 ============
def _format_value(value: Any) -> str:
    """紧凑地渲染字段值，长 JSON 收敛成单行。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _dump_model(obj: Any) -> dict[str, Any]:
    """pydantic v2 优先，失败退回 v1 / getattr 兜底。"""
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:  # pragma: no cover - 理论不会走到
            pass
    if hasattr(obj, "dict"):
        try:
            return obj.dict()
        except Exception:  # pragma: no cover
            pass
    return dict(obj) if isinstance(obj, dict) else {"repr": str(obj)}


def _short(text: str, limit: int = _PREVIEW_LEN) -> str:
    """长文本截断加省略号，用于精简版预览。"""
    return text if len(text) <= limit else text[:limit] + "…"


# ============ 标识符收集（ids 专版 / 精简版共用） ============
async def _collect_identifiers(bot: Bot, event: Event, *, concise: bool = False) -> list[str]:
    """汇总会话内全部标识符。

    同值标签合并为一行「XX、YY同上」（如 C2C 下三种 openid 同值、群聊下
    group_id 与 group_openid 同值）。精简版（concise=True）进一步收拢机器人
    身份为一行、截断超长标识、去掉会话 ID / 事件 ID 等次要项。
    """
    author = getattr(event, "author", None)
    entries: list[tuple[str, str, bool]] = []  # (标签, 值, 精简版允许截断)

    # 机器人自身
    if concise:
        me = None
        if hasattr(bot, "me"):
            try:
                me = await bot.me()
            except Exception as e:
                logger.debug(f"iinfo: bot.me() 失败: {e}")
        if me is not None and getattr(me, "username", None):
            entries.append(("机器人", f"{me.username} (self_id={bot.self_id})", False))
        else:
            entries.append(("机器人 self_id", bot.self_id, False))
    else:
        entries.append(("机器人 self_id", bot.self_id, False))
        if hasattr(bot, "me"):
            try:
                me = await bot.me()
                entries.append(("机器人 username", getattr(me, "username", None) or "", False))
                entries.append(("机器人 id", getattr(me, "id", None) or "", False))
                union = getattr(me, "union_openid", None)
                if union:
                    entries.append(("机器人 union_openid", union, False))
            except Exception as e:
                logger.debug(f"iinfo: bot.me() 失败: {e}")

    # 会话/用户标识
    if not concise:
        entries.append(("适配器会话 ID", event.get_session_id(), False))
    entries.append(("用户 ID (get_user_id)", event.get_user_id(), False))
    group_openid = getattr(event, "group_openid", None)
    if group_openid is not None:
        entries.append(("群 openid (group_openid)", group_openid, False))
    for label, field in (
        ("用户 openid (user_openid)", "user_openid"),
        ("成员 openid (member_openid)", "member_openid"),
        ("union openid (union_openid)", "union_openid"),
        ("author.id", "id"),
    ):
        value = getattr(author, field, None) if author is not None else None
        if value not in (None, ""):
            entries.append((label, str(value), False))
    for label, field in (
        ("群号 (group_id)", "group_id"),
        ("频道 (guild_id)", "guild_id"),
        ("子频道 (channel_id)", "channel_id"),
    ):
        value = getattr(event, field, None)
        if value not in (None, ""):
            entries.append((label, str(value), False))

    # 消息标识
    message_id = getattr(event, "id", None) or getattr(event, "message_id", None)
    if message_id:
        entries.append(("消息 ID", str(message_id), True))
    # msg_idx：顶层字段或 message_scene.ext 中的 key=value
    msg_idx = getattr(event, "msg_idx", None)
    if not msg_idx:
        scene = getattr(event, "message_scene", None)
        for ext in getattr(scene, "ext", None) or []:
            if isinstance(ext, str) and ext.startswith("msg_idx="):
                msg_idx = ext[len("msg_idx=") :]
                break
    if msg_idx:
        entries.append(("msg_idx (被动回复序号)", str(msg_idx), True))
    event_id = getattr(event, "event_id", None)
    if event_id and not concise:
        entries.append(("事件 ID (event_id)", str(event_id), True))

    # 同值分组：首个标签展示值，后续标签合并为一行「XX、YY同上」
    grouped: list[tuple[str, str, list[str]]] = []  # (值, 首个标签, 后续标签)
    index: dict[str, int] = {}
    for label, value, shorten in entries:
        if not value:
            continue
        if concise and shorten:
            value = _short(value)
        if value in index:
            grouped[index[value]][2].append(label)
        else:
            index[value] = len(grouped)
            grouped.append((value, label, []))
    lines: list[str] = []
    for value, label, dups in grouped:
        lines.append(f"{label}: {value}")
        if dups:
            lines.append("、".join(dups) + "同上")
    return lines


# ============ 消息结构（msg 专版） ============
def _collect_message_structure(event: Event) -> list[str]:
    lines: list[str] = []
    lines.append(f"纯文本: {event.get_plaintext() or '(空)'}")
    try:
        message = event.get_message()
        types = [seg.type for seg in message]
        lines.append(f"消息段 ({len(types)} 个): {types}")
        for seg in message:
            if seg.type != "text":
                lines.append(f"  - {seg.type}: {_short(_format_value(seg.data))}")
    except Exception as e:
        logger.debug(f"iinfo: get_message 失败: {e}")
        lines.append(f"消息段: 不可用 ({e})")

    content = getattr(event, "content", None)
    if content is not None:
        lines.append(f"原始 content: {_short(str(content))}")

    mentions = getattr(event, "mentions", None)
    if mentions:
        for m in mentions:
            name = getattr(m, "username", None) or (
                "@全体" if getattr(m, "scope", None) == "all" else "?"
            )
            uid = getattr(m, "member_openid", None) or getattr(m, "id", None) or ""
            lines.append(f"mention: {name} ({uid})")

    reply = getattr(event, "reply", None)
    if reply is not None:
        reply_author = getattr(reply, "author", None)
        lines.append(
            f"引用消息: msg_idx={getattr(reply, 'msg_idx', None)} "
            f"author={getattr(reply_author, 'id', None) if reply_author else None} "
            f"content={_short(str(getattr(reply, 'content', '') or ''))}"
        )

    attachments = getattr(event, "attachments", None)
    if attachments:
        for idx, att in enumerate(attachments, 1):
            lines.append(
                f"attachment#{idx}: type={getattr(att, 'content_type', None)} "
                f"name={getattr(att, 'filename', None)} "
                f"size={getattr(att, 'size', None)} "
                f"尺寸={getattr(att, 'width', None)}x{getattr(att, 'height', None)} "
                f"url={getattr(att, 'url', None)}"
            )
    scene = getattr(event, "message_scene", None)
    if scene is not None:
        lines.append(f"message_scene: {_format_value(_dump_model(scene))}")
    return lines


# ============ API 探测（api 专版） ============
async def _collect_api_probe(bot: Bot, event: Event) -> list[str]:
    api_lines: list[str] = []
    if hasattr(bot, "get_login_info"):
        try:
            api_lines.append(f"登录信息 (get_login_info): {await bot.get_login_info()}")
        except Exception as e:
            logger.debug(f"iinfo: get_login_info 失败: {e}")
    if hasattr(bot, "me"):
        try:
            api_lines.append(f"机器人身份 (GET /users/@me): {await bot.me()}")
        except Exception as e:
            logger.debug(f"iinfo: bot.me() 失败: {e}")
    group_id = getattr(event, "group_id", None)
    if group_id is not None and hasattr(bot, "get_group_info"):
        try:
            group_info = await bot.get_group_info(group_id=group_id)
            api_lines.append(f"群信息 (get_group_info): {group_info}")
        except Exception as e:
            logger.debug(f"iinfo: get_group_info 失败: {e}")
    guild_id = getattr(event, "guild_id", None)
    if guild_id is not None and hasattr(bot, "get_guild"):
        try:
            guild_info = await bot.get_guild(guild_id=str(guild_id))
            api_lines.append(f"频道信息 (get_guild): {guild_info}")
        except Exception as e:
            logger.debug(f"iinfo: get_guild 失败: {e}")
    if not api_lines:
        api_lines.append("(当前适配器无可探测的 API)")
    supported = [
        name
        for name in ("get_login_info", "me", "get_group_info", "get_guild", "get_channels", "get_member")
        if hasattr(bot, name)
    ]
    api_lines.append(f"bot 支持的探测 API: {', '.join(supported) or '(无)'}")
    return api_lines


# ============ 完整版 ============
def _probe_qq_official(event: Event) -> list[str]:
    """QQ 官方 Bot（nonebot-adapter-qq）专属信息段；字段探测不到则整段省略。"""
    author = getattr(event, "author", None)
    is_qq_message = getattr(event, "group_openid", None) is not None or (
        author is not None
        and (
            getattr(author, "member_openid", None) is not None
            or getattr(author, "user_openid", None) is not None
        )
    )
    guild_id = getattr(event, "guild_id", None)
    if not is_qq_message and guild_id is None:
        return []

    lines: list[str] = []
    if is_qq_message:
        lines.append("—— QQ 官方 Bot 字段（openid 体系）——")
        group_openid = getattr(event, "group_openid", None)
        if group_openid is not None:
            lines.append(f"群 openid (group_openid): {group_openid}")
        if author is not None:
            for field in _PROBE_QQ_AUTHOR_FIELDS:
                value = getattr(author, field, None)
                if value not in (None, ""):
                    lines.append(f"author.{field}: {value}")
        for field in ("id", "timestamp", "msg_idx", "message_type", "message_scene"):
            value = getattr(event, field, None)
            if value is not None:
                lines.append(f"message.{field}: {_format_value(value)}")
        lines += _collect_message_structure(event)
    elif guild_id is not None:
        lines.append("—— QQ 官方 Bot 字段（频道 guild 体系）——")
        lines.append(f"频道 (guild_id): {guild_id}")
        channel_id = getattr(event, "channel_id", None)
        if channel_id is not None:
            lines.append(f"子频道 (channel_id): {channel_id}")
    return lines


def _probe_generic_fields(event: Event) -> list[str]:
    probed: dict[str, object] = {
        field: getattr(event, field)
        for field in _PROBE_EVENT_FIELDS
        if getattr(event, field, None) is not None
    }
    sender = getattr(event, "sender", None)
    for field in _PROBE_SENDER_FIELDS:
        value = getattr(sender, field, None)
        if value not in (None, ""):
            probed[f"sender.{field}"] = value
    if not probed:
        return []
    lines = ["—— 适配器特有字段（探测到才显示）——"]
    lines += [f"{key}: {value}" for key, value in probed.items()]
    return lines


def _generic_summary(bot: Bot, event: Event) -> list[str]:
    return [
        "—— 通用信息（NoneBot 核心抽象，所有适配器一致）——",
        f"适配器: {bot.adapter.get_name()}",
        f"机器人 ID (self_id): {bot.self_id}",
        f"事件类型 (get_type): {event.get_type()}",
        f"事件名 (get_event_name): {event.get_event_name()}",
        f"事件描述 (get_event_description): {event.get_event_description()}",
        f"会话 ID (get_session_id): {event.get_session_id()}",
        f"用户 ID (get_user_id): {event.get_user_id()}",
        f"消息@机器人 (is_tome): {event.is_tome()}",
        f"纯文本 (get_plaintext): {event.get_plaintext() or '(空)'}",
    ]


# ============ 处理器 ============
def _parse_mode(event: Event) -> str:
    """从「[前缀]iinfo [模式]」里解析模式，默认 concise。"""
    text = event.get_plaintext().strip()
    text = re.sub(r"^[。/！!]*", "", text)
    rest = re.sub(r"^iinfo\s*", "", text, flags=re.IGNORECASE).strip().lower()
    if not rest:
        return "concise"
    return {"full": "full", "完整": "full", "ids": "ids", "msg": "msg", "api": "api"}.get(
        rest, "concise"
    )


@iinfo.handle()
async def handle_iinfo(bot: Bot, event: Event, matcher: Matcher) -> None:
    mode = _parse_mode(event)

    # 精简版：标识符 + 会话要点，单条回复
    if mode == "concise":
        ids = await _collect_identifiers(bot, event, concise=True)
        concise = ["—— iinfo 精简版 ——", *ids]
        author = getattr(event, "author", None)
        role = getattr(author, "member_role", None) if author is not None else None
        if role:
            concise.append(f"成员身份 (member_role): {role}")
        concise.append(f"事件: {event.get_event_name()}  @我: {event.is_tome()}")
        concise.append(f"纯文本: {_short(event.get_plaintext() or '(空)')}")
        concise.append("(完整版: iinfo full | 标识符: iinfo ids | 消息: iinfo msg | API: iinfo api)")
        await matcher.send("\n".join(concise))
        return

    # 标识符专版
    if mode == "ids":
        ids = await _collect_identifiers(bot, event)
        await matcher.send("\n".join(["—— iinfo 标识符专版 ——", *ids]))
        return

    # 消息结构专版
    if mode == "msg":
        lines = ["—— iinfo 消息结构专版 ——", *_collect_message_structure(event)]
        await matcher.send("\n".join(lines))
        return

    # API 探测专版
    if mode == "api":
        api_lines = await _collect_api_probe(bot, event)
        await matcher.send("\n".join(["—— iinfo API 探测专版 ——", *api_lines]))
        return

    # 完整版
    lines = _generic_summary(bot, event)
    lines += _probe_qq_official(event)
    lines += _probe_generic_fields(event)
    api_lines = await _collect_api_probe(bot, event)
    if api_lines:
        lines.append("")
        lines.append("—— 适配器 API 补充信息 ——")
        lines += api_lines
    await matcher.send("\n".join(lines))

    raw = json.dumps(_dump_model(event), ensure_ascii=False, indent=2, default=str)
    chunks = [raw[i : i + _CHUNK_SIZE] for i in range(0, len(raw), _CHUNK_SIZE)]
    for idx, chunk in enumerate(chunks, 1):
        await matcher.send(f"完整事件 JSON ({idx}/{len(chunks)}):\n{chunk}")
