"""对阵图渲染：把 Tournament 转成 HTML / 文本。

HTML 交给 AstrBot 的 T2I（html_render）转成图片；文本用于群内快速查看。

样式参考 CS:GO Major 淘汰赛对阵树：深绿底、横向选手卡、L 形连接线、
每列是「晋级到该轮」的选手，最后一列是冠军。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .bracket import pending_matches
from .models import (
    MATCH_FINISHED,
    MATCH_READY,
    MATCH_WAITING,
    STATUS_FINISHED,
    STATUS_REGISTRATION,
    STATUS_RUNNING,
    Match,
    Tournament,
)

# ────────── 对阵图几何参数（px） ──────────
CARD_W = 196
CARD_H = 40
CARD_GAP = 10  # 同一场比赛两张卡之间的间距
MATCH_STEP = 116  # 第一轮相邻比赛的垂直间距
COL_GAP = 54  # 相邻两列（卡右缘 -> 下一列卡左缘）的间距
LEFT_PAD = 18
TOP_PAD = 40  # 轮次标题下方，第一张卡的位置
RIGHT_PAD = 18
BOTTOM_PAD = 22
LABEL_TOP = 8


class BracketRenderer:
    """负责把赛事对象渲染为 HTML 或纯文本对阵表。"""

    def __init__(self, template_dir: str | Path):
        self.template_dir = Path(template_dir)
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )

    # ────────── HTML ──────────
    def render_html(
        self,
        tournament: Tournament,
        theme: str = "major",
        avatar_map: dict[str, str] | None = None,
    ) -> str:
        template = self.env.get_template("major_bracket.html")
        return template.render(**self.build_context(tournament, avatar_map=avatar_map))

    def build_context(
        self,
        tournament: Tournament,
        avatar_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        pending = pending_matches(tournament)
        if tournament.status == STATUS_REGISTRATION:
            status_text = f"报名中 {tournament.player_count}/{tournament.size}"
        elif tournament.status == STATUS_RUNNING:
            status_text = f"进行中 · 待判定 {len(pending)} 场"
        elif tournament.status == STATUS_FINISHED:
            status_text = "已结束"
        else:
            status_text = str(tournament.status)

        return {
            "name": tournament.name or "MAJOR 锦标赛",
            "status_text": status_text,
            "size": tournament.size,
            "player_count": tournament.player_count,
            "updated_at": tournament.updated_at,
            "card_w": CARD_W,
            "card_h": CARD_H,
            "b": self.build_bracket_layout(tournament, avatar_map=avatar_map),
        }

    # ────────── 对阵树布局 ──────────
    def build_bracket_layout(
        self,
        tournament: Tournament,
        avatar_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """计算每一列卡片的位置与连接线，供模板绝对定位使用。

        列 0 = 首轮全部选手（每场两张卡）；列 j = 晋级到第 j 轮的选手；
        最后一列 = 冠军。相邻列之间用 L 形连线连接。
        """
        rounds = tournament.rounds
        if not rounds:
            return {
                "width": 0,
                "height": 0,
                "label_top": LABEL_TOP,
                "columns": [],
                "connectors": [],
            }

        col_pitch = CARD_W + COL_GAP

        # 第一列每场比赛两张卡的 top
        first_matches = len(rounds[0].matches)
        prev_tops: list[float] = []
        for i in range(first_matches):
            base = TOP_PAD + i * MATCH_STEP
            prev_tops.append(float(base))
            prev_tops.append(float(base + CARD_H + CARD_GAP))

        columns: list[dict[str, Any]] = []

        def make_column(
            index: int,
            name: str,
            matches: list[Match],
            tops: list[float],
            is_champion: bool = False,
        ) -> dict[str, Any]:
            cards = []
            for match_index, match in enumerate(matches):
                cards.append(
                    self._bracket_card(
                        tournament,
                        match,
                        match.p1,
                        top=tops[2 * match_index],
                        avatar_map=avatar_map,
                    )
                )
                cards.append(
                    self._bracket_card(
                        tournament,
                        match,
                        match.p2,
                        top=tops[2 * match_index + 1],
                        avatar_map=avatar_map,
                    )
                )
            return {
                "name": name,
                "x": LEFT_PAD + index * col_pitch,
                "cards": cards,
                "is_champion": is_champion,
            }

        columns.append(make_column(0, rounds[0].name, rounds[0].matches, prev_tops))

        for index in range(1, len(rounds)):
            # 下一列第 i 张卡对齐上一列第 2i/2i+1 两张卡的中点
            current_tops = [
                (prev_tops[2 * i] + prev_tops[2 * i + 1]) / 2
                for i in range(len(prev_tops) // 2)
            ]
            columns.append(
                make_column(
                    index, rounds[index].name, rounds[index].matches, current_tops
                )
            )
            prev_tops = current_tops

        # 冠军列
        champion_top = (prev_tops[0] + prev_tops[1]) / 2
        champion_card = self._champion_card(tournament, champion_top, avatar_map)
        columns.append(
            {
                "name": "冠军",
                "x": LEFT_PAD + len(rounds) * col_pitch,
                "cards": [champion_card],
                "is_champion": True,
            }
        )

        # 连接线
        connectors: list[str] = []
        for index in range(len(columns) - 1):
            x_right = columns[index]["x"] + CARD_W
            x_mid = x_right + COL_GAP / 2
            x_next = columns[index + 1]["x"]
            tops = [card["top"] for card in columns[index]["cards"]]
            for i in range(len(tops) // 2):
                y_top = tops[2 * i] + CARD_H / 2
                y_bot = tops[2 * i + 1] + CARD_H / 2
                y_mid = (y_top + y_bot) / 2
                connectors.append(f"M {x_right:g} {y_top:g} H {x_mid:g}")
                connectors.append(f"M {x_right:g} {y_bot:g} H {x_mid:g}")
                connectors.append(f"M {x_mid:g} {y_top:g} V {y_bot:g}")
                connectors.append(f"M {x_mid:g} {y_mid:g} H {x_next:g}")

        max_top = max(
            (card["top"] for column in columns for card in column["cards"]),
            default=0.0,
        )
        width = columns[-1]["x"] + CARD_W + RIGHT_PAD
        height = max_top + CARD_H + BOTTOM_PAD
        return {
            "width": round(width),
            "height": round(height),
            "label_top": LABEL_TOP,
            "columns": columns,
            "connectors": connectors,
        }

    def _bracket_card(
        self,
        tournament: Tournament,
        match: Match | None,
        user_id: str | None,
        *,
        top: float,
        avatar_map: dict[str, str] | None = None,
        champion: bool = False,
    ) -> dict[str, Any]:
        if not user_id:
            return {
                "top": top,
                "empty": True,
                "name": "待定",
                "initial": "?",
                "avatar": None,
                "seed": "",
                "score": "",
                "win": False,
                "lose": False,
                "champion": False,
            }

        player = tournament.get_player(user_id)
        name = player.name if player else str(user_id)
        score = ""
        win = lose = False
        if match is not None and match.winner:
            win = match.winner == user_id
            lose = not win
            raw = match.score1 if match.p1 == user_id else match.score2
            score = "" if raw is None else str(raw)
        if champion:
            # 冠军用独立的金色样式，不叠加胜负样式
            win = lose = False

        return {
            "top": top,
            "empty": False,
            "name": name,
            "initial": self._initial(name),
            "avatar": (avatar_map or {}).get(user_id),
            "seed": player.seed if player and player.seed else "",
            "score": score,
            "win": win,
            "lose": lose,
            "champion": champion,
        }

    def _champion_card(
        self,
        tournament: Tournament,
        top: float,
        avatar_map: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if not tournament.champion:
            return {
                "top": top,
                "empty": True,
                "name": "虚位以待",
                "initial": "?",
                "avatar": None,
                "seed": "",
                "score": "",
                "win": False,
                "lose": False,
                "champion": False,
            }
        return self._bracket_card(
            tournament,
            None,
            tournament.champion,
            top=top,
            avatar_map=avatar_map,
            champion=True,
        )

    @staticmethod
    def _initial(name: str | None) -> str:
        text = str(name or "").strip()
        return text[:1] if text else "?"

    # ────────── 纯文本 ──────────
    def render_text(self, tournament: Tournament) -> str:
        lines: list[str] = []
        title = tournament.name or "MAJOR 锦标赛"
        lines.append(f"🏆 {title} · {tournament.size}强单败淘汰赛")
        if tournament.status == STATUS_REGISTRATION:
            if tournament.size > 0:
                progress = f"{tournament.player_count}/{tournament.size}"
            else:
                progress = f"{tournament.player_count} 人（规模开赛时自动确定）"
            lines.append(f"状态：报名中（{progress}）")
            for index, player in enumerate(tournament.players, start=1):
                lines.append(f"  {index}. {player.name}")
            lines.append("报名完成后请管理员发送「major 开赛」。")
            return "\n".join(lines)

        for round_ in tournament.rounds:
            lines.append(f"\n【{round_.name}】")
            for match in round_.matches:
                p1 = tournament.player_name(match.p1)
                p2 = tournament.player_name(match.p2)
                if match.status == MATCH_FINISHED:
                    mark = "✅"
                    score = ""
                    if match.score1 is not None or match.score2 is not None:
                        score = f" ({match.score1 or 0}:{match.score2 or 0})"
                    winner = tournament.player_name(match.winner)
                    lines.append(
                        f"  {mark} {match.match_id} {p1} vs {p2}{score} → {winner}"
                    )
                elif match.status == MATCH_WAITING:
                    lines.append(f"  ⏳ {match.match_id} {p1} vs {p2}（等待上一轮）")
                elif match.status == MATCH_READY:
                    lines.append(f"  🎮 {match.match_id} {p1} vs {p2}  ← 待判定")
                else:
                    lines.append(f"  ·  {match.match_id} {p1} vs {p2}")

        if tournament.champion:
            lines.append(f"\n🏆 冠军：{tournament.player_name(tournament.champion)}")
        elif tournament.status == STATUS_RUNNING:
            pending = pending_matches(tournament)
            if pending:
                lines.append(f"\n提示：还有 {len(pending)} 场待人工判定。")
        return "\n".join(lines)
