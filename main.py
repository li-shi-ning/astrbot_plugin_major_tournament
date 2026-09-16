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

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

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
    STATUS_FINISHED,
    STATUS_REGISTRATION,
    STATUS_RUNNING,
    Tournament,
)
from .core.qq_official_buttons import (
    add_passive_reply_context,
    build_panel_payload,
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
        self.renderer = BracketRenderer(Path(__file__).parent / "templates")

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
            "【参赛】\n"
            "  major 报名            报名参赛\n"
            "  major 退赛            取消报名\n"
            "  major 名单            查看报名名单\n"
            "【开赛 / 查看】\n"
            "  major 开赛 [规模] [名称] 开赛；不填规模按人数自动确定\n"
            "  major 命名 <名称>     修改比赛名称（管理员）\n"
            "  major 重抽            重新随机抽签（开赛后、未有结果前）\n"
            "  major 记录 [数量]     查询最近的比赛结果记录\n"
            "  major 对阵            文字版赛程\n"
            "  major 图              渲染 Major 对阵图\n"
            "【人工判定】（管理员）\n"
            "  major 胜 <编号> <1|2|名字> [比分]\n"
            "     例：major 胜 R16-3 1 2:1\n"
            "  major 添加 <账号> [名字]   帮他人报名\n"
            "  major 重置            删除当前赛事\n"
            "———————————————\n"
            "提示：赛程开赛后，每场由管理员手动判胜，胜者自动晋级。"
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
            if await self._send_button_panel(event, self._load_or_new(event)):
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

        if action == "报名":
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
        elif action == "重置":
            async for item in self._handle_reset(event):
                yield item
        else:
            yield event.plain_result(
                f"❓ 未知指令「{tokens[0]}」。发送「major 帮助」查看用法。"
            )
            return

        # 报名/退赛/开赛/重置/判胜后自动刷新按钮面板
        if action in {"报名", "退赛", "开赛", "重置", "胜", "命名", "重抽"}:
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
        if await self._send_button_panel(event, self._load_or_new(event)):
            return
        yield event.plain_result(
            "❌ 按钮面板发送失败，可发送「major 帮助」查看文字指令。"
        )

    # ────────────────────────── 按钮面板 ──────────────────────────
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
        self, event: AstrMessageEvent, tournament: Tournament
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

        payload = build_panel_payload(tournament, is_admin=event.is_admin())
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
            logger.warning(f"[Major] 发送按钮面板失败: {exc}")
            return False

        event.stop_event()
        return True

    # ────────────────────────── 各子命令 ──────────────────────────
    async def _handle_signup(self, event: AstrMessageEvent, args: list[str]):
        user_id = str(event.get_sender_id() or "").strip()
        sender_name = str(event.get_sender_name() or "").strip() or user_id
        if args:  # 允许报名时带上自定义名字
            sender_name = args[0]

        tournament = self._load(event)
        if tournament is None:
            tournament = self._new(event)

        if tournament.status != STATUS_REGISTRATION:
            yield event.plain_result("❌ 当前赛事已开赛，无法报名。请等待管理员重置。")
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
            f"✅ 报名成功：{sender_name}\n"
            f"{progress}"
            f"准备就绪后由管理员发送「major 开赛」。"
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
            yield event.plain_result("ℹ️ 当前没有赛事。发送「major 报名」开始报名。")
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
        if not event.is_admin():
            yield event.plain_result("❌ 只有管理员可以重新抽签。")
            return
        tournament = self._load(event)
        if tournament is None:
            yield event.plain_result("ℹ️ 当前没有赛事。")
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
        if not event.is_admin():
            yield event.plain_result("❌ 只有管理员可以修改比赛名称。")
            return
        name = " ".join(args).strip()
        if not name:
            yield event.plain_result(
                "用法：major 命名 <比赛名称>\n例：major 命名 群友 MAJOR 杯"
            )
            return
        tournament = self._load(event)
        if tournament is None:
            tournament = self._new(event)
        tournament.name = name[:30]
        self._save(tournament)
        yield event.plain_result(f"✅ 比赛名称已设置为「{tournament.name}」。")

    async def _handle_history(self, event: AstrMessageEvent, args: list[str]):
        """从数据库查询最近的比赛结果。"""
        limit = 10
        for arg in args:
            if arg.isdigit():
                limit = min(50, max(1, int(arg)))
                break

        rows = self.store.recent_history(
            self._group_id(event), self._platform_id(event), limit
        )
        if not rows:
            yield event.plain_result("ℹ️ 暂无比赛记录。")
            return

        lines = [f"📜 最近 {len(rows)} 场比赛记录："]
        for row in rows:
            score = ""
            if row.get("score1") is not None or row.get("score2") is not None:
                score = f"（{row.get('score1') or 0}:{row.get('score2') or 0}）"
            lines.append(
                f"  [{row.get('round_name')}] {row.get('match_id')} "
                f"{row.get('winner_name')} 胜 {row.get('loser_name')}{score}"
            )
        lines.append("完整赛程与结果保存在插件数据库 major.db。")
        yield event.plain_result("\n".join(lines))

    async def _handle_add(self, event: AstrMessageEvent, args: list[str]):
        if not event.is_admin():
            yield event.plain_result("❌ 只有管理员可以使用「major 添加」。")
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

        tournament = self._load(event)
        if tournament is None:
            tournament = self._new(event)
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
        if not event.is_admin():
            yield event.plain_result("❌ 只有管理员可以开赛。")
            return

        tournament = self._load(event)
        if tournament is None or not tournament.players:
            yield event.plain_result("❌ 还没有人报名，无法开赛。")
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

        normalized = normalize_size(size, tournament.player_count)
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
            result = await self._render_bracket_image(tournament)
        except Exception as exc:  # noqa: BLE001 - 渲染失败时回退文字
            yield event.plain_result(
                f"❌ 对阵图渲染失败：{exc}\n可先发送「major 对阵」查看文字版。"
            )
            return

        async for item in self._yield_bracket_image(event, result):
            yield item

    async def _render_bracket_image(self, tournament: Tournament):
        """调用 AstrBot T2I 渲染对阵图，返回 bytes 或 URL/路径。"""
        html = self.renderer.render_html(tournament)
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
        if not event.is_admin():
            yield event.plain_result("❌ 只有管理员可以判定胜负。")
            return
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
                result = await self._render_bracket_image(tournament)
                async for item in self._yield_bracket_image(event, result):
                    yield item
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"⚠️ 自动对阵图渲染失败：{exc}")

    async def _handle_reset(self, event: AstrMessageEvent):
        if not event.is_admin():
            yield event.plain_result("❌ 只有管理员可以重置赛事。")
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
