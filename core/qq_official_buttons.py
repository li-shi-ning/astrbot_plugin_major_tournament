"""QQ 官方机器人按钮面板构造（纯函数，方便单测）。

只使用普通 dict 组装 payload；botpy 的 ``Keyboard`` / ``Button`` /
``MarkdownPayload`` 都是 TypedDict，普通 dict 即可被 SDK 接受。

按钮统一使用 ``action.type = 2`` 的「指令按钮」：点击后会把
``action.data`` 当作一条 @机器人 的消息发出来，因此 data 直接写成
插件已有的命令（如 ``major 报名`` / ``major 胜 R16-1 1``）即可复用命令逻辑，
不依赖 INTERACTION_CREATE 回调。
"""

from __future__ import annotations

import random
from typing import Any

from .bracket import has_decided_real_match, pending_matches
from .models import (
    STATUS_FINISHED,
    STATUS_REGISTRATION,
    STATUS_RUNNING,
    Match,
    Tournament,
)

#: 支持的 QQ 官方平台标识
QQ_OFFICIAL_PLATFORMS = {"qq_official", "qq_official_webhook"}

#: QQ 键盘最多 5 行
MAX_ROWS = 5

#: 按钮 label 建议长度
LABEL_LIMIT = 10


def is_qq_official_platform(platform_name: str | None) -> bool:
    return str(platform_name or "").strip().lower() in QQ_OFFICIAL_PLATFORMS


def _truncate(text: str, limit: int = LABEL_LIMIT) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def build_command_button(
    button_id: str,
    label: str,
    data: str,
    *,
    visited_label: str | None = None,
    permission: dict[str, Any] | None = None,
    style: int = 1,
    enter: bool = False,
) -> dict[str, Any]:
    """构造一个 ``action.type = 2`` 的 QQ 官方指令按钮。

    ``enter=True`` 时按钮为「输入框按钮」：点击后不直接发送，而是把 data
    填入输入框，用户可继续补充参数（用于「开赛 名称 规模」这种带参指令）。
    """
    return {
        "id": button_id,
        "render_data": {
            "label": _truncate(label),
            "visited_label": _truncate(visited_label or label),
            "style": style,
        },
        "action": {
            "type": 2,
            "permission": dict(permission or {"type": 2}),
            "data": data,
            "reply": True,
            "enter": enter,
            "unsupport_tips": "当前客户端不支持该按钮",
        },
    }


def build_status_markdown(tournament: Tournament, room_exists: bool = True) -> str:
    """生成按钮面板上方的说明文字（Markdown）。"""
    if not room_exists:
        return (
            "🏠 还没有创建房间\n"
            "房主点「🏠 创建房间」新建比赛，可继续补「名称 规模」\n"
            "创建后大家点「📝 报名」加入"
        )

    title = tournament.name or "MAJOR 锦标赛"
    lines = [f"🏆 {title}"]
    if tournament.status == STATUS_REGISTRATION:
        if tournament.size > 0:
            progress = f"{tournament.player_count}/{tournament.size}"
        else:
            progress = f"{tournament.player_count} 人（规模开赛时自动确定）"
        lines.append(f"报名中：{progress}　点击下方按钮即可参赛")
        lines.append("准备就绪后，房主点「🚀 开赛」即可开始")
    elif tournament.status == STATUS_RUNNING:
        pending = pending_matches(tournament)
        lines.append(f"进行中 · 待判定 {len(pending)} 场")
        if pending:
            first = pending[0]
            lines.append(
                f"下一场：{first.match_id} "
                f"{tournament.player_name(first.p1)} vs "
                f"{tournament.player_name(first.p2)}"
            )
    elif tournament.status == STATUS_FINISHED:
        lines.append(f"已结束 · 冠军 {tournament.player_name(tournament.champion)}")
    else:
        lines.append(str(tournament.status))
    size_text = f"{tournament.size} 强" if tournament.size > 0 else "开赛时自动确定"
    lines.append(f"选手 {tournament.player_count} 人 · 规模 {size_text}")
    return "\n".join(lines)


def _winner_buttons(
    tournament: Tournament, match: Match, offset: int
) -> list[dict[str, Any]]:
    label1 = _truncate(f"{match.match_id} {tournament.player_name(match.p1)}", 12)
    label2 = _truncate(f"{match.match_id} {tournament.player_name(match.p2)}", 12)
    return [
        build_command_button(
            f"major_win_{offset}_1",
            label1,
            f"major 胜 {match.match_id} 1",
            visited_label="已判定",
            style=1,
        ),
        build_command_button(
            f"major_win_{offset}_2",
            label2,
            f"major 胜 {match.match_id} 2",
            visited_label="已判定",
            style=1,
        ),
    ]


def build_panel_keyboard(
    tournament: Tournament,
    *,
    can_manage: bool = False,
    is_admin: bool | None = None,
    room_exists: bool = True,
    max_winner_rows: int = 2,
) -> dict[str, Any]:
    """按赛事状态生成按钮键盘。

    - ``room_exists=False``：还没创建房间，只显示「🏠 创建房间」。
    - ``can_manage``：触发面板的人是否有管理权限（房主或管理员）。
    - 「🚀 开赛」按钮始终显示，方便房主随时开赛。
    """
    if is_admin is not None:  # 兼容旧参数名
        can_manage = can_manage or is_admin
    """按赛事状态生成按钮键盘。"""
    rows: list[dict[str, list[dict[str, Any]]]] = []
    registration_open = tournament.status == STATUS_REGISTRATION

    signup = build_command_button(
        "major_signup", "📝 报名", "major 报名", visited_label="已报名"
    )
    leave = build_command_button(
        "major_leave", "🚪 退赛", "major 退赛", visited_label="已退赛"
    )
    players = build_command_button("major_players", "👥 名单", "major 名单")
    bracket_btn = build_command_button("major_bracket", "📋 赛程", "major 对阵")
    image_btn = build_command_button("major_image", "🖼 对阵图", "major 图")
    help_btn = build_command_button("major_help", "❓ 帮助", "major 帮助")
    history_btn = build_command_button("major_history", "📜 记录", "major 记录")
    redraw_btn = build_command_button(
        "major_redraw", "🎲 重抽", "major 重抽", visited_label="已重抽"
    )
    reset_btn = build_command_button(
        "major_reset", "🗑 重置", "major 重置", visited_label="已重置"
    )
    start_btn = build_command_button(
        "major_start", "🚀 开赛", "major 开赛", visited_label="已开赛"
    )
    create_room_btn = build_command_button(
        "major_create_room",
        "🏠 创建房间",
        "major 创建房间 ",
        visited_label="已创建",
        enter=True,
    )

    if not room_exists:
        # 还没创建房间：只提供创建入口
        rows.append({"buttons": [create_room_btn]})
        rows.append({"buttons": [help_btn]})
    elif registration_open:
        rows.append({"buttons": [signup, leave, players]})
        rows.append({"buttons": [bracket_btn, image_btn, history_btn, help_btn]})
        if can_manage:
            rows.append({"buttons": [start_btn, reset_btn]})
        else:
            # 开赛按钮始终显示，方便房主直接开赛（点击时再做权限校验）
            rows.append({"buttons": [start_btn]})
    else:
        rows.append({"buttons": [players, bracket_btn, image_btn]})
        if can_manage:
            for offset, match in enumerate(
                pending_matches(tournament)[:max_winner_rows]
            ):
                rows.append({"buttons": _winner_buttons(tournament, match, offset)})
            if has_decided_real_match(tournament):
                rows.append({"buttons": [reset_btn, history_btn, help_btn]})
            else:
                rows.append({"buttons": [redraw_btn, reset_btn]})
                rows.append({"buttons": [history_btn, help_btn]})
        else:
            rows.append({"buttons": [history_btn, help_btn]})

    return {"content": {"rows": rows[:MAX_ROWS]}}


def build_panel_payload(
    tournament: Tournament,
    *,
    can_manage: bool = False,
    is_admin: bool | None = None,
    room_exists: bool = True,
    max_winner_rows: int = 2,
) -> dict[str, Any]:
    """构造完整的 QQ 官方按钮消息 payload（未包含被动回复上下文）。"""
    return {
        "msg_type": 2,
        "markdown": {
            "content": build_status_markdown(tournament, room_exists=room_exists)
        },
        "keyboard": build_panel_keyboard(
            tournament,
            can_manage=can_manage,
            is_admin=is_admin,
            room_exists=room_exists,
            max_winner_rows=max_winner_rows,
        ),
    }


def add_passive_reply_context(
    payload: dict[str, Any],
    *,
    msg_id: str | None = None,
    event_id: str | None = None,
    msg_seq: int | None = None,
) -> dict[str, Any]:
    """补充被动回复上下文，避免按钮消息被平台当作主动消息拦截。"""
    if msg_id:
        payload["msg_id"] = str(msg_id)
    elif event_id:
        payload["event_id"] = str(event_id)
    if payload.get("msg_id") or payload.get("event_id"):
        payload["msg_seq"] = (
            int(msg_seq) if msg_seq is not None else random.randint(1, 10000)
        )
    return payload


def extract_message_reference_id(raw_message: Any, message_obj: Any) -> str | None:
    """优先用原始消息 id，其次用 message_obj.message_id。"""
    for value in (
        getattr(raw_message, "id", None),
        getattr(message_obj, "message_id", None),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return None
