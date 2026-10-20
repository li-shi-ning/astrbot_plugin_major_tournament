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

import asyncio
import base64
import re
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from PIL import Image

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
    "冠军榜": "冠军榜",
    "榜单": "冠军榜",
    "冠军列表": "冠军榜",
    "历届冠军": "冠军榜",
    "champions": "冠军榜",
    "名片": "名片",
    "我的名片": "名片",
    "战绩卡": "名片",
    "我的战绩": "名片",
    "profile": "名片",
    "清理": "清理",
    "清理记录": "清理",
    "清理未完赛": "清理",
    "purge": "清理",
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
            "  major 冠军榜          历届冠军榜单图片\n"
            "  major 名片 [@某人]     个人获奖名片图片\n"
            "  major 清理            清理未完赛的脏记录（管理员）\n"
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
        elif action == "冠军榜":
            async for item in self._handle_champion_list(event):
                yield item
        elif action == "名片":
            async for item in self._handle_profile(event, args):
                yield item
        elif action == "清理":
            async for item in self._handle_purge(event):
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
            return
        # 本局已结束/归档：直接给「创建房间」面板，方便开下一局
        await self._send_button_panel(event, self._new(event), room_exists=False)

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
            if existing.status == STATUS_FINISHED:
                # 兼容历史数据：已结束但还没归档的赛事，直接归档后开新房
                self.store.delete(self._group_id(event), self._platform_id(event))
            else:
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

    RECORDS_PAGE_SIZE = 4

    @staticmethod
    def _format_time(raw: str) -> str:
        text = str(raw or "")
        try:
            from datetime import datetime

            return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return text[:16]

    def _record_display_name(self, row: dict[str, Any]) -> str:
        """赛事显示名；老数据没有名字时用创建日期兜底。"""
        name = str(row.get("name") or "").strip()
        if name:
            return name
        created = self._format_time(str(row.get("created_at") or ""))
        date_part = created[5:10].replace("-", "") if len(created) >= 10 else ""
        return f"未命名{date_part}" if date_part else "未命名赛事"

    @staticmethod
    def _record_status_text(status: str) -> str:
        return {
            STATUS_REGISTRATION: "报名中",
            STATUS_RUNNING: "进行中",
            STATUS_FINISHED: "已结束",
        }.get(status, str(status))

    @staticmethod
    def _now_text() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    async def _handle_champion_list(self, event: AstrMessageEvent):
        """历届冠军榜（图片）。"""
        records = [
            row
            for row in self.store.list_tournaments(
                self._group_id(event), self._platform_id(event), limit=50, offset=0
            )
            if row.get("champion")
        ]
        if not records:
            yield event.plain_result("ℹ️ 还没有产生冠军的比赛。")
            return

        for row in records:
            row["champion_name"] = self._pretty_name(
                str(row.get("champion") or ""), row.get("champion_name", "")
            )

        avatar_map = await self._fetch_avatars(
            [str(row["champion"]) for row in records], event
        )
        html = self.renderer.render_champion_list(
            records, avatar_map, updated_at=self._now_text()
        )
        width, height = self.renderer.champion_list_size(len(records))
        try:
            result = await self._render_html_image(html, width, height)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"❌ 冠军榜渲染失败：{exc}")
            return
        async for item in self._yield_bracket_image(event, result):
            yield item

    def _resolve_profile_target(self, event: AstrMessageEvent, args: list[str]) -> str:
        """名片查询目标：优先 @某人（已排除机器人自己），其次参数，最后发送者。"""
        mentions = self._extract_mentions(event)
        if mentions:
            return mentions[0]
        if args and args[0].strip():
            return self._strip_mention_markup(args[0])
        return str(event.get_sender_id() or "")

    async def _handle_profile(self, event: AstrMessageEvent, args: list[str]):
        """个人获奖名片（图片）。"""
        user_id = self._resolve_profile_target(event, args)
        if not user_id:
            yield event.plain_result("❌ 无法确定要查询的成员。")
            return

        summary = self.store.player_summary(
            self._group_id(event), self._platform_id(event), user_id
        )
        sender_id = str(event.get_sender_id() or "")
        # 昵称兜底：@ 的昵称 > 发送者昵称 > 群友xxxx（避免直接显示 openid）
        fallback = self._mention_names(event).get(user_id, "")
        if user_id == sender_id:
            fallback = fallback or str(event.get_sender_name() or "")
        summary["name"] = self._pretty_name(user_id, summary.get("name", ""), fallback)

        avatar_map = await self._fetch_avatars([user_id], event)
        html = self.renderer.render_profile(
            summary, avatar_map, updated_at=self._now_text()
        )
        width, height = self.renderer.profile_card_size(
            len(summary.get("titles") or []), len(summary.get("runnerups") or [])
        )
        try:
            result = await self._render_html_image(html, width, height)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"❌ 名片渲染失败：{exc}")
            return
        async for item in self._yield_bracket_image(event, result):
            yield item

    async def _handle_purge(self, event: AstrMessageEvent):
        """清理未完赛的脏记录（管理员）。"""
        if not event.is_admin():
            yield event.plain_result("❌ 只有管理员可以清理比赛记录。")
            return
        removed = self.store.purge_unfinished(
            self._group_id(event), self._platform_id(event)
        )
        if removed:
            yield event.plain_result(f"🧹 已清理 {removed} 场未完赛的记录。")
        else:
            yield event.plain_result("ℹ️ 没有需要清理的未完赛记录。")

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
        names = [self._record_display_name(row) for row in rows]
        for index, row in enumerate(rows):
            number = offset + index + 1
            name = names[index]
            # 同一页出现重名时，用序号区分按钮
            label = f"{name}#{number}" if names.count(name) > 1 else name
            entries.append({"n": number, "id": row["tournament_id"], "label": label})
            champion = row.get("champion_name") or ""
            suffix = f" · 冠军 {champion}" if champion else ""
            lines.append(
                f"{number}. {name} · "
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
        title = tournament.name or "MAJOR 锦标赛"
        lines = [
            f"🏆 {title}",
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
            targets = [self._strip_mention_markup(args[0])]
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

    async def _fetch_avatars(
        self, user_ids: list[str], event: AstrMessageEvent
    ) -> dict[str, str]:
        """下载一批用户头像，返回 {user_id: data_uri}。"""
        ids = [str(uid) for uid in user_ids if uid]
        if not ids:
            return {}
        platform_name = self._platform_name(event)
        appid = self._resolve_appid(event)
        urls = [build_avatar_url(uid, platform_name, appid) for uid in ids]
        if not any(urls):
            return {}
        data_uris = await self.avatar_fetcher.fetch_many(urls)
        return {
            uid: data_uri
            for uid, data_uri in zip(ids, data_uris, strict=False)
            if data_uri
        }

    async def _collect_avatar_map(
        self, tournament: Tournament, event: AstrMessageEvent
    ) -> dict[str, str]:
        """下载所有选手头像，返回 {user_id: data_uri}。"""
        return await self._fetch_avatars(
            [player.user_id for player in tournament.players], event
        )

    #: device_scale_factor_level -> 大致像素倍率（用于估算图片大小）
    _SCALE_RATIOS = {"normal": 1.0, "high": 1.35, "ultra": 1.8}

    #: 单张图片允许的最大像素数，超过就自动降一档，避免图片过大
    _MAX_IMAGE_PIXELS = 8_000_000

    def _pick_render_scales(self, layout: dict[str, Any]) -> list[str]:
        """按配置与内容大小挑渲染档位（清晰优先，过大自动降档）。

        返回按优先级排列的 device_scale_factor_level 列表，逐个尝试。
        """
        requested = (
            str(self.config.get("image_scale", "ultra") or "ultra").strip().lower()
        )
        order = ["ultra", "high", "normal"]
        if requested not in order:
            requested = "ultra"
        candidates = order[order.index(requested) :]
        base_pixels = max(1, int(layout.get("width", 0)) * int(layout.get("height", 0)))
        affordable = [
            scale
            for scale in candidates
            if base_pixels * self._SCALE_RATIOS[scale] ** 2 <= self._MAX_IMAGE_PIXELS
        ]
        return affordable or [candidates[-1]]

    @staticmethod
    def _is_valid_image(result: Any) -> bool:
        """判断 T2I 返回的是不是真图片（远端偶发会返回错误页）。"""
        if isinstance(result, (bytes, bytearray)):
            head = bytes(result[:16])
        else:
            try:
                with open(result, "rb") as handle:
                    head = handle.read(16)
            except (OSError, TypeError):
                return False
        return bool(head) and (
            head.startswith((b"\xff\xd8", b"\x89PNG\r\n\x1a\n", b"GIF8"))
            or (head.startswith(b"RIFF") and b"WEBP" in head)
        )

    async def _render_html_image(self, html: str, width: int, height: int):
        """把 HTML 交给 T2I 渲染成图片。

        清晰度优先；渲染失败、**或返回的不是图片**（远端偶发返回错误页）时
        自动降档重试，最后再兜底重试一次。
        """
        layout = {"width": max(1, int(width)), "height": max(1, int(height))}
        scales = self._pick_render_scales(layout)
        attempts = scales + [scales[-1]]  # 末尾再兜一次，抗远端偶发失败

        last_error: Exception | None = None
        for index, scale in enumerate(attempts, start=1):
            options = {
                "type": "png",
                "full_page": True,
                "quality": 100,
                "device_scale_factor_level": scale,
                "timeout": 120000,
            }
            try:
                result = await self.html_render(
                    html, {}, return_url=False, options=options
                )
            except Exception as exc:  # noqa: BLE001 - 高清失败则降档重试
                last_error = exc
                logger.warning(
                    f"[Major] T2I 第 {index} 次渲染({scale})异常，降档重试: {exc}"
                )
                continue
            if result and self._is_valid_image(result):
                return result
            last_error = RuntimeError("T2I 返回的不是有效图片")
            logger.warning(
                f"[Major] T2I 第 {index} 次渲染({scale})返回非图片，降档重试"
            )
        if last_error is not None:
            raise last_error
        return None

    async def _render_bracket_image(
        self, tournament: Tournament, event: AstrMessageEvent
    ):
        """渲染对阵图（带头像）。"""
        avatar_map = await self._collect_avatar_map(tournament, event)
        html = self.renderer.render_html(tournament, avatar_map=avatar_map)
        layout = self.renderer.build_bracket_layout(tournament, avatar_map=avatar_map)
        return await self._render_html_image(
            html, layout["width"] + 32, layout["height"] + 108
        )

    @staticmethod
    async def _yield_bracket_image(event: AstrMessageEvent, result):
        """Send images with one original retry and one compressed fallback.

        Args:
            event: Event whose adapter performs the actual upload and send.
            result: Rendered image bytes, local path, or HTTP URL.

        Yields:
            A text error if the image is invalid or all send attempts fail.
        """
        blob = None
        if isinstance(result, bytes):
            blob = result
        elif isinstance(result, str) and result:
            if not result.startswith(("http://", "https://")):
                try:
                    blob = await asyncio.to_thread(Path(result).read_bytes)
                except OSError:
                    # Let the adapter resolve paths it can access itself.
                    pass
        else:
            yield event.plain_result("❌ 对阵图渲染失败，请稍后重试。")
            return

        if blob is not None and not _looks_like_image(blob):
            yield event.plain_result("❌ 对阵图渲染结果不是有效图片，请检查 T2I 服务。")
            return

        for attempt in range(2):
            try:
                message = (
                    event.make_result().base64_image(base64.b64encode(blob).decode())
                    if blob is not None
                    else event.image_result(result)
                )
                # Yielding would defer upload until after this exception handler.
                await event.send(message)
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    f"[Major] Original image send attempt {attempt + 1}/2 failed: {exc}"
                )

        try:
            if blob is None:
                if result.startswith(("http://", "https://")):
                    from astrbot.core.utils.io import download_image_by_url

                    downloaded = Path(await download_image_by_url(result))
                    try:
                        blob = await asyncio.to_thread(downloaded.read_bytes)
                    finally:
                        await asyncio.to_thread(downloaded.unlink, missing_ok=True)
                else:
                    blob = await asyncio.to_thread(Path(result).read_bytes)

            def compress_image() -> bytes:
                """Encode a smaller JPEG without modifying the original file.

                Returns:
                    JPEG bytes, reducing dimensions when quality alone is insufficient.
                """
                with Image.open(BytesIO(blob)) as source:
                    source.load()
                    image = Image.new("RGB", source.size, "white")
                    rgba = source.convert("RGBA")
                    image.paste(rgba, mask=rgba.getchannel("A"))
                    image.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
                    while True:
                        for quality in (80, 60, 40):
                            output = BytesIO()
                            image.save(
                                output, format="JPEG", quality=quality, optimize=True
                            )
                            compressed = output.getvalue()
                            if len(compressed) < len(blob) or image.size == (1, 1):
                                return compressed
                        image = image.resize(
                            (max(1, image.width // 2), max(1, image.height // 2)),
                            Image.Resampling.LANCZOS,
                        )

            compressed = await asyncio.to_thread(compress_image)
            await event.send(
                event.make_result().base64_image(base64.b64encode(compressed).decode())
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[Major] Compressed image fallback failed: {exc}")
            yield event.plain_result(
                "❌ 图片上传失败，原图已重试并尝试压缩，请稍后重试。"
            )

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
        if tournament.status == STATUS_FINISHED:
            # 冠军已产生：自动归档本局，方便直接开下一局（战绩仍保留在数据库里）
            self.store.delete(self._group_id(event), self._platform_id(event))
            yield event.plain_result(
                f"✅ {message}\n"
                "🏁 本局已结束并自动归档，可直接「major 创建房间」开启下一局；"
                "历史战绩用「major 记录」查看。"
            )
        else:
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
    def _collect_mentions(event: AstrMessageEvent) -> list[tuple[str, str]]:
        """收集消息里被 @ 的成员 [(user_id, name)]，跳过 @全体 和机器人自己。

        两种来源都要处理：
        - AstrBot 解析出的 ``At`` 组件；
        - 部分平台（QQ 官方）会把 @ 保留成纯文本 ``<@openid>``。
        """
        try:
            from astrbot.core.message.components import At, Plain
        except Exception:  # noqa: BLE001
            return []

        self_id = str(event.get_self_id() or "").strip()
        found: list[tuple[str, str]] = []
        seen: set[str] = set()

        def keep(qq: str, name: str) -> None:
            uid = str(qq or "").strip()
            if not uid or uid == "all" or (self_id and uid == self_id):
                return
            if uid in seen:
                return
            seen.add(uid)
            found.append((uid, str(name or "").strip()))

        for component in event.get_messages():
            if isinstance(component, At):
                keep(getattr(component, "qq", ""), getattr(component, "name", ""))
            elif isinstance(component, Plain):
                for match in re.finditer(
                    r"<@!?([0-9A-Za-z_\-]{6,})>", getattr(component, "text", "") or ""
                ):
                    keep(match.group(1), "")
        return found

    @classmethod
    def _extract_mentions(cls, event: AstrMessageEvent) -> list[str]:
        """提取被 @ 的账号（排除机器人自己）。"""
        return [uid for uid, _name in cls._collect_mentions(event)]

    @classmethod
    def _mention_names(cls, event: AstrMessageEvent) -> dict[str, str]:
        """被 @ 成员的昵称映射 {user_id: name}。"""
        names: dict[str, str] = {}
        for uid, name in cls._collect_mentions(event):
            if name:
                names.setdefault(uid, name)
        return names

    @staticmethod
    def _strip_mention_markup(text: str) -> str:
        """把 ``<@openid>`` / ``<@!openid>`` 还原成裸 user_id。"""
        raw = str(text or "").strip()
        match = re.fullmatch(r"<@!?([0-9A-Za-z_\-]{6,})>", raw)
        return match.group(1) if match else raw

    @staticmethod
    def _looks_like_openid(text: str) -> bool:
        return bool(re.fullmatch(r"[0-9A-Fa-f]{20,}", str(text or "").strip()))

    def _pretty_name(self, user_id: str, name: str, fallback: str = "") -> str:
        """昵称缺失/等于 openid 时，用可读的名称兜底。"""
        text = str(name or "").strip()
        uid = str(user_id or "").strip()
        if text and text != uid:
            return text
        if fallback and str(fallback).strip() and str(fallback).strip() != uid:
            return str(fallback).strip()
        if self._looks_like_openid(uid):
            return f"群友{uid[:6]}"
        return text or uid
