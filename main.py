"""Major 赛制插件。

功能：
- 群内报名
- 管理员人工判定每场胜负
- 自动生成 32/16/8/4/2 强单败淘汰赛程（标准种子排位 + 轮空晋级）
- 参考 T2I 方案渲染 Major 风格对阵图
- QQ 官方机器人支持按钮面板（报名 / 退赛 / 名单 / 赛程 / 判胜等）

命令统一入口：major（别名：锦标赛 / major赛 / major比赛）
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .core.avatars import AvatarFetcher, build_avatar_url
from .core.bracket import (
    MAX_SIZE,
    VALID_SIZES,
    normalize_size,
    pending_matches,
    redraw,
    set_winner,
    start_tournament,
)
from .core.database import TournamentDatabase
from .core.models import (
    MATCH_FINISHED,
    MATCH_READY,
    STATUS_FINISHED,
    STATUS_REGISTRATION,
    STATUS_RUNNING,
    Tournament,
)
from .core.qq_official_buttons import (
    add_passive_reply_context,
    build_detail_payload,
    build_panel_payload,
    build_records_payload,
    extract_message_reference_id,
    is_qq_official_platform,
)
from .core.renderer import BracketRenderer

PLUGIN_NAME = "astrbot_plugin_major_tournament"

#: 主命令及其别名
COMMAND_NAMES = ("major", "锦标赛", "major赛", "major比赛", "major锦标赛")

#: 子命令别名归一化
ACTION_ALIASES = {
    "help": "帮助",
    "帮助": "帮助",
    "菜单": "帮助",
    "signup": "报名",
    "join": "报名",
    "报名": "报名",
    "quit": "退赛",
    "leave": "退赛",
    "退赛": "退赛",
    "取消报名": "退赛",
    "名单": "名单",
    "列表": "名单",
    "list": "名单",
    "players": "名单",
    "add": "添加",
    "添加": "添加",
    "start": "开赛",
    "开赛": "开赛",
    "对阵": "对阵",
    "赛程": "对阵",
    "bracket": "对阵",
    "图": "图",
    "图片": "图",
    "对阵图": "图",
    "image": "图",
    "render": "图",
    "胜": "胜",
    "判胜": "胜",
    "win": "胜",
    "重置": "重置",
    "reset": "重置",
    "删除": "重置",
    "命名": "命名",
    "改名": "命名",
    "名称": "命名",
    "name": "命名",
    "rename": "命名",
    "记录": "记录",
    "战绩": "记录",
    "历史": "记录",
    "history": "记录",
    "详情": "详情",
    "记录详情": "详情",
    "detail": "详情",
    "详情图": "详情图",
    "记录图": "详情图",
    "detailimage": "详情图",
    "创建房间": "创建房间",
    "建房": "创建房间",
    "创建比赛": "创建房间",
    "创建": "创建房间",
    "create": "创建房间",
    "newroom": "创建房间",
    "重抽": "重抽",
    "重新抽签": "重抽",
    "重新抽": "重抽",
    "抽签": "重抽",
    "redraw": "重抽",
    "shuffle": "重抽",
}


def _normalize_action(raw: str) -> str:
    text = str(raw or "").strip().lower()
    return ACTION_ALIASES.get(text, text)


def _looks_like_image(blob: bytes) -> bool:
    """通过文件头判断是否为常见图片格式。"""
    if not blob:
        return False
    return blob.startswith((b"\xff\xd8", b"\x89PNG\r\n\x1a\n", b"GIF8")) or (
        blob.startswith(b"RIFF") and b"WEBP" in blob[:16]
    )


class MajorTournament(Star):
    """Major 赛制插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = TournamentDatabase(self.data_dir / "major.db")
        self.avatar_fetcher = AvatarFetcher()
        self.renderer = BracketRenderer(Path(__file__).parent / "templates")

    async def terminate(self) -> None:
        """插件卸载时释放头像下载会话。"""
        try:
            await self.avatar_fetcher.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[Major] 关闭头像会话失败: {exc}")

    # ────────────────────────── 工具方法 ──────────────────────────
    @staticmethod
    def _group_id(event: AstrMessageEvent) -> str:
        return str(event.get_group_id() or "").strip()

    @staticmethod
    def _platform_id(event: AstrMessageEvent) -> str:
        return str(event.get_platform_id() or "").strip()

    def _load(self, event: AstrMessageEvent) -> Tournament | None:
        group_id = self._group_id(event)
        if not group_id:
            return None
        return self.store.load(group_id, self._platform_id(event))

    def _save(self, tournament: Tournament) -> None:
        self.store.save(tournament)

    def _new(self, event: AstrMessageEvent) -> Tournament:
        return Tournament(
            group_id=self._group_id(event),
            platform_id=self._platform_id(event),
            size=int(self.config.get("default_size", 0) or 0),
            name=str(self.config.get("default_name", "") or ""),
            creator_id=str(event.get_sender_id() or ""),
        )

    @staticmethod
    def _raw_args(event: AstrMessageEvent) -> str:
        """取出「主命令名」之后的参数文本。"""
        text = re.sub(r"\s+", " ", str(event.get_message_str() or "").strip())
        for name in sorted(COMMAND_NAMES, key=len, reverse=True):
            if text == name:
                return ""
            if text.startswith(f"{name} "):
                return text[len(name) + 1 :].strip()
        return text

    @staticmethod
    def _status_line(tournament: Tournament) -> str:
        if tournament.status == STATUS_REGISTRATION:
            if tournament.size > 0:
                return f"报名中（{tournament.player_count}/{tournament.size}）"
            return f"报名中（{tournament.player_count} 人，规模开赛时自动确定）"
        if tournament.status == STATUS_RUNNING:
            return f"进行中 · 待判定 {len(pending_matches(tournament))} 场"
        if tournament.status == STATUS_FINISHED:
            return f"已结束 · 冠军 {tournament.player_name(tournament.champion)}"
        return tournament.status

    @staticmethod
    def _size_text(tournament: Tournament) -> str:
        if tournament.size > 0:
            return f"{tournament.size} 强"
        return "开赛时自动确定"

    @staticmethod
    def _draw_text(tournament: Tournament, limit: int = 16) -> str:
        ordered = sorted(tournament.players, key=lambda p: p.seed or 999)
        shown = "、".join(f"{p.seed}.{p.name}" for p in ordered[:limit])
        if len(ordered) > limit:
            shown += f" 等 {len(ordered)} 人"
        return shown

    def _help_text(self) -> str:
        return (
            "🏆 Major 赛制锦标赛\n"
            "———————————————\n"
            "【房间 / 参赛】\n"
            "  major 创建房间 [名称] [规模]  创建房间（规模可省略，开赛时自动）\n"
            "  major 报名            报名参赛\n"
            "  major 退赛            取消报名\n"
            "  major 名单            查看报名名单\n"
            "【开赛 / 查看】\n"
            "  major 开赛 [名称] [规模] 开赛（规模可选，放最后）\n"
            "  major 命名 <名称>     修改比赛名称（房主/管理员）\n"
            "  major 重抽            重新随机抽签（房主/管理员）\n"
            "  major 记录 [页码]     比赛记录列表（按钮翻页、查看详情）\n"
            "  major 对阵            文字版赛程\n"
            "  major 图              渲染 Major 对阵图\n"
            "【人工判定】（房主/管理员）\n"
            "  major 胜 <编号> <1|2|名字> [比分]\n"
            "     例：major 胜 R16-3 1 2:1\n"
            "  major 添加 <账号> [名字]   帮他人报名\n"
            "  major 重置            删除当前赛事\n"
            "———————————————\n"
            "房主 = 第一个创建赛事（首次报名/命名）的人。\n"
            "开赛按钮始终显示，但只有房主或管理员可以开赛；重置同样仅限房主/管理员。"
        )

    # ────────────────────────── 主命令 ──────────────────────────
    @filter.command("major", alias={"锦标赛", "major赛", "major比赛", "major锦标赛"})
    async def major(self, event: AstrMessageEvent):
        """Major 赛制锦标赛主命令。"""
        event.should_call_llm(True)  # 阻止 LLM 重复回复

        tokens = self._raw_args(event).split()
        action = _normalize_action(tokens[0]) if tokens else ""
        args = tokens[1:]

        if not action:
            # QQ 官方机器人：优先发按钮面板；其它平台回退文字帮助
            loaded = self._load(event)
            tournament = loaded if loaded is not None else self._new(event)
            if await self._send_button_panel(
                event, tournament, room_exists=loaded is not None
            ):
                return
            yield event.plain_result(self._help_text())
            return

        if action == "帮助":
            yield event.plain_result(self._help_text())
            return

        group_id = self._group_id(event)
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用 Major 赛制命令。")
            return

        if action == "创建房间":
            async for item in self._handle_create_room(event, args):
                yield item
        elif action == "报名":
            async for item in self._handle_signup(event, args):
                yield item
        elif action == "退赛":
            async for item in self._handle_leave(event):
                yield item
        elif action == "名单":
            async for item in self._handle_players(event):
                yield item
        elif action == "添加":
            async for item in self._handle_add(event, args):
                yield item
        elif action == "开赛":
            async for item in self._handle_start(event, args):
                yield item
        elif action == "对阵":
            async for item in self._handle_bracket_text(event):
                yield item
        elif action == "图":
            async for item in self._handle_bracket_image(event):
                yield item
        elif action == "胜":
            async for item in self._handle_winner(event, args):
                yield item
        elif action == "命名":
            async for item in self._handle_rename(event, args):
                yield item
        elif action == "重抽":
            async for item in self._handle_redraw(event, args):
                yield item
        elif action == "记录":
            async for item in self._handle_history(event, args):
                yield item
        elif action == "详情":
            async for item in self._handle_record_detail(event, args):
                yield item
        elif action == "详情图":
            async for item in self._handle_record_image(event, args):
                yield item
        elif action == "重置":
            async for item in self._handle_reset(event):
                yield item
        else:
            yield event.plain_result(
                f"❓ 未知指令「{tokens[0]}」。发送「major 帮助」查看用法。"
            )
            return

        # 报名/退赛/开赛/重置/判胜后自动刷新按钮面板
        if action in {
            "创建房间",
            "报名",
            "退赛",
            "开赛",
            "重置",
            "胜",
            "命名",
            "重抽",
        }:
            await self._maybe_send_button_panel(event)

    @filter.command("major面板", alias={"major按钮", "major菜单", "major_menu"})
    async def major_panel(self, event: AstrMessageEvent):
        """单独发送 Major 按钮面板（QQ 官方机器人）。"""
        event.should_call_llm(True)
        group_id = self._group_id(event)
        if not group_id:
            yield event.plain_result("❌ 请在群聊中使用按钮面板。")
            return
        if not bool(self.config.get("buttons_enabled", True)):
            yield event.plain_result("ℹ️ 按钮功能已在配置中关闭。")
            return
        if not is_qq_official_platform(self._platform_name(event)):
            yield event.plain_result(
                "ℹ️ 按钮面板仅支持 QQ 官方机器人；其它平台请发送「major 帮助」。"
            )
            return
        loaded = self._load(event)
        tournament = loaded if loaded is not None else self._new(event)
        if await self._send_button_panel(
            event, tournament, room_exists=loaded is not None
        ):
            return
        yield event.plain_result(
            "❌ 按钮面板发送失败，可发送「major 帮助」查看文字指令。"
        )

    # ────────────────────────── 按钮面板 ──────────────────────────
    def _ensure_host(self, event: AstrMessageEvent, tournament: Tournament) -> bool:
        """是否为房主或管理员。

        房主 = 创建赛事的人；管理员始终可以管理。
        兼容没有 creator_id 的旧赛事：第一个来管理的人自动成为房主。
        """
        if event.is_admin():
            return True
        sender = str(event.get_sender_id() or "").strip()
        creator = str(getattr(tournament, "creator_id", "") or "").strip()
        if not creator:
            tournament.creator_id = sender
            return True
        return bool(sender) and sender == creator

    def _load_or_new(self, event: AstrMessageEvent) -> Tournament:
        tournament = self._load(event)
        if tournament is None:
            tournament = self._new(event)
        return tournament

    @staticmethod
    def _platform_name(event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_platform_name", None)
        if callable(getter):
            try:
                return str(getter() or "")
            except Exception:  # noqa: BLE001
                return ""
        return str(getattr(event, "platform_name", "") or "")

    async def _maybe_send_button_panel(self, event: AstrMessageEvent) -> None:
        if not bool(self.config.get("button_auto_refresh", True)):
            return
        tournament = self._load(event)
        if tournament is not None:
            await self._send_button_panel(event, tournament)

    async def _send_button_panel(
        self, event: AstrMessageEvent, tournament: Tournament, room_exists: bool = True
    ) -> bool:
        """发送赛事面板（Markdown + 按钮键盘）。"""
        payload = build_panel_payload(
            tournament,
            can_manage=self._ensure_host(event, tournament),
            room_exists=room_exists,
        )
        return await self._send_markdown_keyboard(event, payload)

    async def _send_markdown_keyboard(
        self, event: AstrMessageEvent, payload: dict[str, Any]
    ) -> bool:
        """通过 QQ 官方 API 发送 Markdown + 按钮键盘。成功返回 True。"""
        if not bool(self.config.get("buttons_enabled", True)):
            return False
        if not is_qq_official_platform(self._platform_name(event)):
            return False

        message_obj = getattr(event, "message_obj", None)
        raw_message = getattr(message_obj, "raw_message", None)
        api = getattr(getattr(event, "bot", None), "api", None)
        if raw_message is None or api is None:
            return False

        add_passive_reply_context(
            payload,
            msg_id=extract_message_reference_id(raw_message, message_obj),
            msg_seq=getattr(raw_message, "msg_seq", None),
        )
        try:
            group_openid = getattr(raw_message, "group_openid", None)
            if group_openid:
                await api.post_group_message(group_openid=group_openid, **payload)
            else:
                author = getattr(raw_message, "author", None)
                user_openid = getattr(author, "user_openid", None)
                if not user_openid:
                    return False
                await api.post_c2c_message(openid=user_openid, **payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Major] 发送按钮消息失败: {exc}")
            return False

        event.stop_event()
        return True

    # ────────────────────────── 各子命令 ──────────────────────────
    async def _handle_create_room(self, event: AstrMessageEvent, args: list[str]):
        existing = self._load(event)
        if existing is not None:
            yield event.plain_result(
                "❌ 当前已有房间，如需重建请先「major 重置」（房主/管理员）。"
            )
            return

        size = None
        name_parts: list[str] = []
        for arg in args:
            if arg.isdigit():
                size = int(arg)
            else:
                name_parts.append(arg)
        if size is not None and size not in VALID_SIZES:
            yield event.plain_result(
                "❌ 规模只能是 2/4/8/16/32/64（不填则开赛时自动确定）。"
            )
            return

        tournament = self._new(event)
        name = " ".join(name_parts).strip()
        if name:
            tournament.name = name[:30]
        if not tournament.name:
            tournament.name = "MAJOR 锦标赛"
        tournament.size = size or 0
        tournament.status = STATUS_REGISTRATION
        self._save(tournament)

        size_text = (
            f"{tournament.size} 强" if tournament.size > 0 else "开赛时按人数自动确定"
        )
        host_name = str(event.get_sender_name() or event.get_sender_id())
        yield event.plain_result(
            f"🏠 房间已创建：{tournament.name}\n"
            f"房主：{host_name}\n"
            f"规模：{size_text}\n"
            f"大家发送「major 报名」或点「📝 报名」加入；"
            f"人齐后房主点「🚀 开赛」。"
        )

    async def _handle_signup(self, event: AstrMessageEvent, args: list[str]):
        user_id = str(event.get_sender_id() or "").strip()
        sender_name = str(event.get_sender_name() or "").strip() or user_id
        if args:  # 允许报名时带上自定义名字
            sender_name = args[0]

        tournament = self._load(event)
        if tournament is None:
            yield event.plain_result(
                "❌ 还没有房间。请房主先发送「major 创建房间 [名称] [规模]」。"
            )
            return

        if tournament.status != STATUS_REGISTRATION:
            yield event.plain_result("❌ 当前赛事已开赛，无法报名。请等待重置。")
            return
        if not tournament.add_player(user_id, sender_name):
            if tournament.find_player_index(user_id) >= 0:
                yield event.plain_result(f"✅ {sender_name} 已经报名过了。")
            else:
                yield event.plain_result(
                    f"❌ 报名人数已满（{tournament.player_count}/{tournament.size}）。"
                )
            return

        self._save(tournament)
        if tournament.size > 0:
            progress = f"当前 {tournament.player_count}/{tournament.size} 人。"
        else:
            progress = f"当前 {tournament.player_count} 人（规模开赛时自动确定）。"
        yield event.plain_result(
            f"✅ 报名成功：{sender_name}\n{progress}准备就绪后由房主点「🚀 开赛」。"
        )

    async def _handle_leave(self, event: AstrMessageEvent):
        tournament = self._load(event)
        if tournament is None:
            yield event.plain_result("ℹ️ 当前没有进行中的赛事。")
            return
        if tournament.status != STATUS_REGISTRATION:
            yield event.plain_result("❌ 已开赛，无法退赛。")
            return
        user_id = str(event.get_sender_id() or "").strip()
        if not tournament.remove_player(user_id):
            yield event.plain_result("ℹ️ 你还没有报名。")
            return
        self._save(tournament)
        if tournament.size > 0:
            progress = f"当前 {tournament.player_count}/{tournament.size} 人。"
        else:
            progress = f"当前 {tournament.player_count} 人。"
        yield event.plain_result(f"✅ 已退赛。{progress}")

    async def _handle_players(self, event: AstrMessageEvent):
        tournament = self._load(event)
        if tournament is None:
            yield event.plain_result(
                "ℹ️ 当前没有房间。请房主先「major 创建房间 [名称] [规模]」。"
            )
            return
        if not tournament.players:
            yield event.plain_result("ℹ️ 还没有人报名。")
            return
        lines = [
            f"🏆 {tournament.name or 'Major 锦标赛'} · {self._status_line(tournament)}",
            f"规模：{self._size_text(tournament)}",
            "———————————————",
        ]
        if tournament.status == STATUS_REGISTRATION:
            lines.append("报名序号（开赛时随机抽签）：")
            for index, player in enumerate(tournament.players, start=1):
                lines.append(f"  {index}. {player.name}")
        else:
            lines.append("抽签种子（随机）：")
            for player in sorted(tournament.players, key=lambda p: p.seed or 999):
                lines.append(f"  种子{player.seed}. {player.name}")
        yield event.plain_result("\n".join(lines))

    async def _handle_redraw(self, event: AstrMessageEvent, args: list[str]):
        tournament = self._load(event)
        if tournament is None:
            yield event.plain_result("ℹ️ 当前没有赛事。")
            return
        if not self._ensure_host(event, tournament):
            yield event.plain_result("❌ 只有房主或管理员可以重新抽签。")
            return
        seed_mode = (
            "register" if any(a in {"报名", "register"} for a in args) else "random"
        )
        ok, message = redraw(tournament, seed_mode=seed_mode)
        if not ok:
            yield event.plain_result(f"❌ {message}")
            return
        self._save(tournament)
        yield event.plain_result(
            f"✅ {message}\n🎲 新抽签结果：{self._draw_text(tournament)}"
        )

    async def _handle_rename(self, event: AstrMessageEvent, args: list[str]):
        tournament = self._load(event)
        if tournament is None:
            yield event.plain_result("❌ 还没有房间。请先「major 创建房间」。")
            return
        if not self._ensure_host(event, tournament):
            yield event.plain_result("❌ 只有房主或管理员可以修改比赛名称。")
            return
        name = " ".join(args).strip()
        if not name:
            yield event.plain_result(
                "用法：major 命名 <比赛名称>\n例：major 命名 群友 MAJOR 杯"
            )
            return
        tournament.name = name[:30]
        self._save(tournament)
        yield event.plain_result(f"✅ 比赛名称已设置为「{tournament.name}」。")

    RECORDS_PAGE_SIZE = 5

    @staticmethod
    def _format_time(raw: str) -> str:
        text = str(raw or "")
        try:
            from datetime import datetime

            return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return text[:16]

    @staticmethod
    def _record_status_text(status: str) -> str:
        return {
            STATUS_REGISTRATION: "报名中",
            STATUS_RUNNING: "进行中",
            STATUS_FINISHED: "已结束",
        }.get(status, str(status))

    async def _handle_history(self, event: AstrMessageEvent, args: list[str]):
        """比赛记录：按钮列表 + 分页。"""
        group_id = self._group_id(event)
        platform_id = self._platform_id(event)
        total = self.store.count_tournaments(group_id, platform_id)
        if total <= 0:
            yield event.plain_result("ℹ️ 暂无比赛记录。")
            return

        page_size = self.RECORDS_PAGE_SIZE
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = 1
        for arg in args:
            if arg.isdigit():
                page = int(arg)
                break
        page = min(max(1, page), total_pages)
        offset = (page - 1) * page_size

        rows = self.store.list_tournaments(
            group_id, platform_id, limit=page_size, offset=offset
        )
        entries: list[dict[str, Any]] = []
        lines = [f"📜 比赛记录（第 {page}/{total_pages} 页 · 共 {total} 场）"]
        for index, row in enumerate(rows):
            number = offset + index + 1
            entries.append({"n": number, "id": row["tournament_id"]})
            name = row["name"] or f"major#{number}"
            champion = row.get("champion_name") or ""
            suffix = f" · 冠军 {champion}" if champion else ""
            lines.append(
                f"major#{number} {name} · "
                f"{self._record_status_text(row['status'])} · "
                f"{row['player_count']}人{suffix}"
            )
        lines.append("点下方按钮查看详情")
        text = "\n".join(lines)

        payload = build_records_payload(
            text, entries, page=page, total_pages=total_pages
        )
        if await self._send_markdown_keyboard(event, payload):
            return
        yield event.plain_result(text)

    def _resolve_record(self, event: AstrMessageEvent, key: str) -> Tournament | None:
        """按钮给的是 tournament_id；文字里也可以用 1-based 序号。"""
        text = str(key or "").strip()
        group_id = self._group_id(event)
        platform_id = self._platform_id(event)
        if text.isdigit() and len(text) <= 4:
            return self.store.tournament_at_rank(group_id, platform_id, int(text))
        return self.store.load_by_id(text)

    def _format_record_detail(self, tournament: Tournament, rank: int) -> str:
        prefix = f"major#{rank}" if rank > 0 else "major#?"
        title = tournament.name or "MAJOR 锦标赛"
        lines = [
            f"🏆 {prefix} · {title}",
            f"状态：{self._record_status_text(tournament.status)}",
            f"创建：{self._format_time(tournament.created_at)}",
            f"参赛人员（{tournament.player_count}）："
            + "、".join(player.name for player in tournament.players),
            f"冠军：{tournament.player_name(tournament.champion) if tournament.champion else '—'}",
            "",
            "比赛赛程：",
        ]
        if not tournament.rounds:
            lines.append("  （尚未开赛）")
        for round_ in tournament.rounds:
            for match in round_.matches:
                p1 = tournament.player_name(match.p1)
                p2 = tournament.player_name(match.p2)
                if match.status == MATCH_FINISHED and match.p1 and match.p2:
                    score = ""
                    if match.score1 is not None or match.score2 is not None:
                        score = f" {match.score1 or 0}:{match.score2 or 0} "
                    else:
                        score = " "
                    lines.append(
                        f"  [{round_.name}] {match.match_id} {p1}{score}{p2}"
                        f"  → {tournament.player_name(match.winner)}"
                    )
                elif match.status == MATCH_FINISHED:
                    lines.append(
                        f"  [{round_.name}] {match.match_id} "
                        f"{tournament.player_name(match.winner)}（轮空晋级）"
                    )
                elif match.status == MATCH_READY:
                    lines.append(
                        f"  [{round_.name}] {match.match_id} {p1} vs {p2}（待判定）"
                    )
                else:
                    lines.append(f"  [{round_.name}] {match.match_id} {p1} vs {p2}")
        text = "\n".join(lines)
        if len(text) > 1800:
            text = text[:1800] + "\n…（内容过长已截断）"
        return text

    async def _handle_record_detail(self, event: AstrMessageEvent, args: list[str]):
        key = args[0] if args else ""
        tournament = self._resolve_record(event, key)
        if tournament is None:
            yield event.plain_result("❌ 找不到该记录，请重新「major 记录」查看列表。")
            return
        rank = self.store.rank_of(
            tournament.tournament_id, self._group_id(event), self._platform_id(event)
        )
        text = self._format_record_detail(tournament, rank)
        payload = build_detail_payload(text, tournament.tournament_id)
        if await self._send_markdown_keyboard(event, payload):
            return
        yield event.plain_result(
            text + f"\n\n发送「major 详情图 {tournament.tournament_id}」查看对阵图。"
        )

    async def _handle_record_image(self, event: AstrMessageEvent, args: list[str]):
        key = args[0] if args else ""
        tournament = self._resolve_record(event, key)
        if tournament is None:
            yield event.plain_result("❌ 找不到该记录。")
            return
        if not tournament.rounds:
            yield event.plain_result("❌ 该记录还没有赛程（未开赛）。")
            return
        try:
            result = await self._render_bracket_image(tournament, event)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"❌ 对阵图渲染失败：{exc}")
            return
        async for item in self._yield_bracket_image(event, result):
            yield item

    async def _handle_add(self, event: AstrMessageEvent, args: list[str]):
        tournament = self._load(event)
        if tournament is None:
            yield event.plain_result("❌ 还没有房间。请房主先创建房间。")
            return
        if not self._ensure_host(event, tournament):
            yield event.plain_result("❌ 只有房主或管理员可以使用「major 添加」。")
            return

        targets = self._extract_mentions(event)
        name = ""
        if args and not targets:
            targets = [args[0]]
            name = args[1] if len(args) > 1 else ""
        elif args and targets:
            name = args[-1]

        if not targets:
            yield event.plain_result(
                "用法：major 添加 <账号> [名字]，或 @某人 后发送 major 添加。"
            )
            return

        if tournament.status != STATUS_REGISTRATION:
            yield event.plain_result("❌ 已开赛，无法添加选手。")
            return

        added: list[str] = []
        for target in targets:
            display = name or target
            if tournament.add_player(target, display):
                added.append(display)
        if not added:
            yield event.plain_result("❌ 没有新增选手（已报名或人数已满）。")
            return
        self._save(tournament)
        if tournament.size > 0:
            progress = f"当前 {tournament.player_count}/{tournament.size} 人。"
        else:
            progress = f"当前 {tournament.player_count} 人。"
        yield event.plain_result(f"✅ 已添加：{'、'.join(added)}。{progress}")

    async def _handle_start(self, event: AstrMessageEvent, args: list[str]):
        tournament = self._load(event)
        if tournament is None or not tournament.players:
            yield event.plain_result("❌ 还没有人报名，无法开赛。")
            return
        if not self._ensure_host(event, tournament):
            yield event.plain_result("❌ 只有房主或管理员可以开赛。")
            return
        if tournament.status == STATUS_RUNNING:
            yield event.plain_result("❌ 赛事已经开始了。发送「major 对阵」查看赛程。")
            return

        size = None
        seed_mode = "random"
        name_parts: list[str] = []
        for arg in args:
            if arg.isdigit():
                size = int(arg)
            elif arg.lower() in {"random", "随机", "register", "报名"}:
                seed_mode = (
                    "register" if arg.lower() in {"register", "报名"} else "random"
                )
            else:
                name_parts.append(arg)
        if name_parts:
            tournament.name = " ".join(name_parts)[:30]

        if tournament.player_count < 2:
            yield event.plain_result("❌ 至少需要 2 名选手才能开赛。")
            return

        # 未在开赛时指定规模 -> 用创建房间时的规模；创建时也没填 -> 自动
        requested_size = size if size is not None else (tournament.size or None)
        normalized = normalize_size(requested_size, tournament.player_count)
        if normalized > MAX_SIZE:
            yield event.plain_result(
                f"❌ 报名 {tournament.player_count} 人，超过单败淘汰赛上限 "
                f"{MAX_SIZE} 人；请分批开赛。"
            )
            return
        if normalized not in VALID_SIZES:
            yield event.plain_result("❌ 规模只能是 2/4/8/16/32/64。")
            return

        start_tournament(tournament, size=normalized, seed_mode=seed_mode)
        self._save(tournament)

        pending = pending_matches(tournament)
        lines = [
            f"🚀 {tournament.name or 'Major 锦标赛'} 开赛！",
            f"规模：{tournament.size} 强 ｜ 选手：{tournament.player_count} 人",
            f"🎲 随机抽签结果：{self._draw_text(tournament)}",
            f"首轮对阵已生成，共 {len(pending)} 场待判定。",
            "发送「major 对阵」查看文字赛程，或「major 图」查看对阵图。",
        ]
        yield event.plain_result("\n".join(lines))

    async def _handle_bracket_text(self, event: AstrMessageEvent):
        tournament = self._load(event)
        if tournament is None:
            yield event.plain_result("ℹ️ 当前没有赛事。")
            return
        if tournament.status == STATUS_REGISTRATION:
            yield event.plain_result("ℹ️ 赛事尚未开始。发送「major 开赛」生成对阵。")
            return
        yield event.plain_result(self.renderer.render_text(tournament))

    async def _handle_bracket_image(self, event: AstrMessageEvent):
        tournament = self._load(event)
        if tournament is None:
            yield event.plain_result("ℹ️ 当前没有赛事。")
            return
        if tournament.status == STATUS_REGISTRATION:
            yield event.plain_result(
                "ℹ️ 赛事尚未开始。发送「major 开赛」后再渲染对阵图。"
            )
            return

        try:
            result = await self._render_bracket_image(tournament, event)
        except Exception as exc:  # noqa: BLE001 - 渲染失败时回退文字
            yield event.plain_result(
                f"❌ 对阵图渲染失败：{exc}\n可先发送「major 对阵」查看文字版。"
            )
            return

        async for item in self._yield_bracket_image(event, result):
            yield item

    def _resolve_appid(self, event: AstrMessageEvent) -> str:
        """解析 QQ 官方机器人的 appid（用于拼头像地址）。"""
        bot = getattr(event, "bot", None)
        platform_config = getattr(getattr(bot, "platform", None), "config", None)
        if isinstance(platform_config, dict) and platform_config.get("appid"):
            return str(platform_config["appid"]).strip()

        context = getattr(self, "context", None)
        manager = getattr(context, "platform_manager", None)
        insts = getattr(manager, "platform_insts", None) or []
        try:
            target_id = str(event.get_platform_id() or "")
        except Exception:  # noqa: BLE001
            target_id = ""

        fallback = ""
        for inst in insts:
            try:
                meta = inst.meta()
                inst_id = str(getattr(meta, "id", "") or "")
            except Exception:  # noqa: BLE001
                inst_id = ""
            config = getattr(inst, "config", None)
            appid = ""
            if isinstance(config, dict) and config.get("appid"):
                appid = str(config["appid"]).strip()
            elif getattr(inst, "appid", None):
                appid = str(inst.appid).strip()
            if not appid:
                continue
            if target_id and inst_id == target_id:
                return appid
            fallback = fallback or appid
        return fallback

    async def _collect_avatar_map(
        self, tournament: Tournament, event: AstrMessageEvent
    ) -> dict[str, str]:
        """下载所有选手头像，返回 {user_id: data_uri}。"""
        platform_name = self._platform_name(event)
        appid = self._resolve_appid(event)
        user_ids = [player.user_id for player in tournament.players]
        urls = [build_avatar_url(user_id, platform_name, appid) for user_id in user_ids]
        if not any(urls):
            return {}
        data_uris = await self.avatar_fetcher.fetch_many(urls)
        return {
            user_id: data_uri
            for user_id, data_uri in zip(user_ids, data_uris, strict=False)
            if data_uri
        }

    async def _render_bracket_image(
        self, tournament: Tournament, event: AstrMessageEvent
    ):
        """调用 AstrBot T2I 渲染对阵图，返回 bytes 或 URL/路径。"""
        avatar_map = await self._collect_avatar_map(tournament, event)
        html = self.renderer.render_html(tournament, avatar_map=avatar_map)
        options = {
            "type": "png",
            "full_page": True,
            "quality": 95,
            "viewport_width": 360 + max(1, len(tournament.rounds)) * 300 + 200,
        }
        return await self.html_render(html, {}, return_url=False, options=options)

    @staticmethod
    async def _yield_bracket_image(event: AstrMessageEvent, result):
        """把渲染结果统一转成 base64 图片再发送。

        兼容不同平台与 T2I 返回类型：
        - bytes：直接 base64；
        - http(s) URL：直接发链接；
        - 本地路径：读取文件后 base64（避免 OneBot / 官方机器人访问不到内部路径）。
        """
        if isinstance(result, bytes):
            if not _looks_like_image(result):
                yield event.plain_result(
                    "❌ 对阵图渲染结果不是有效图片，请检查 T2I 服务。"
                )
                return
            yield event.make_result().base64_image(
                base64.b64encode(result).decode("utf-8")
            )
            return

        if isinstance(result, str) and result.startswith(("http://", "https://")):
            yield event.image_result(result)
            return

        if isinstance(result, str) and result:
            try:
                with open(result, "rb") as handle:
                    blob = handle.read()
            except OSError:
                # 读取失败时退回让平台自行处理该路径
                yield event.image_result(result)
                return
            if not _looks_like_image(blob):
                yield event.plain_result(
                    "❌ 对阵图渲染结果不是有效图片，请检查 T2I 服务。"
                )
                return
            yield event.make_result().base64_image(
                base64.b64encode(blob).decode("utf-8")
            )
            return

        yield event.plain_result("❌ 对阵图渲染失败，请稍后重试。")

    async def _handle_winner(self, event: AstrMessageEvent, args: list[str]):
        if len(args) < 2:
            yield event.plain_result(
                "用法：major 胜 <比赛编号> <1|2|选手名字> [比分]\n"
                "例：major 胜 R16-3 1 2:1"
            )
            return

        tournament = self._load(event)
        if tournament is None:
            yield event.plain_result("ℹ️ 当前没有赛事。")
            return
        if not self._ensure_host(event, tournament):
            yield event.plain_result("❌ 只有房主或管理员可以判定胜负。")
            return

        match_id = args[0]
        winner_key = args[1]
        score1 = score2 = None
        if len(args) >= 3 and re.fullmatch(r"\d+\s*[:：\-]\s*\d+", args[2]):
            parts = re.split(r"[:：\-]", args[2])
            score1, score2 = int(parts[0]), int(parts[1])

        ok, message = set_winner(
            tournament, match_id, winner_key, score1=score1, score2=score2
        )
        if not ok:
            yield event.plain_result(f"❌ {message}")
            return
        self._save(tournament)
        yield event.plain_result(f"✅ {message}")

        if (
            bool(self.config.get("auto_render", False))
            and tournament.status != STATUS_REGISTRATION
        ):
            try:
                result = await self._render_bracket_image(tournament, event)
                async for item in self._yield_bracket_image(event, result):
                    yield item
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"⚠️ 自动对阵图渲染失败：{exc}")

    async def _handle_reset(self, event: AstrMessageEvent):
        tournament = self._load(event)
        if tournament is None:
            yield event.plain_result("ℹ️ 当前没有可删除的赛事。")
            return
        if not self._ensure_host(event, tournament):
            yield event.plain_result("❌ 只有房主或管理员可以重置赛事。")
            return
        deleted = self.store.delete(self._group_id(event), self._platform_id(event))
        if not deleted:
            yield event.plain_result("ℹ️ 当前没有可删除的赛事。")
            return
        yield event.plain_result("🗑️ 已重置当前赛事，可以重新「major 报名」了。")

    @staticmethod
    def _extract_mentions(event: AstrMessageEvent) -> list[str]:
        """从消息链里提取被 @ 的账号。"""
        targets: list[str] = []
        try:
            from astrbot.core.message.components import At
        except Exception:  # noqa: BLE001
            return targets
        for component in event.get_messages():
            if isinstance(component, At):
                qq = str(getattr(component, "qq", "") or "").strip()
                if qq and qq != "all" and qq not in targets:
                    targets.append(qq)
        return targets
