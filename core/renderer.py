"""对阵图渲染：把 Tournament 转成 HTML / 文本。

HTML 交给 AstrBot 的 T2I（html_render）转成图片；文本用于群内快速查看。
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
    def render_html(self, tournament: Tournament, theme: str = "major") -> str:
        template = self.env.get_template("major_bracket.html")
        return template.render(**self.build_context(tournament))

    def build_context(self, tournament: Tournament) -> dict[str, Any]:
        rounds = []
        for round_ in tournament.rounds:
            rounds.append(
                {
                    "name": round_.name,
                    "matches": [
                        self._match_context(tournament, match)
                        for match in round_.matches
                    ],
                }
            )

        pending = pending_matches(tournament)
        if tournament.status == STATUS_REGISTRATION:
            status_text = f"报名中 {tournament.player_count}/{tournament.size}"
        elif tournament.status == STATUS_RUNNING:
            status_text = f"进行中 · 待判定 {len(pending)} 场"
        elif tournament.status == STATUS_FINISHED:
            status_text = "已结束"
        else:
            status_text = str(tournament.status)

        next_hint = ""
        if pending:
            first = pending[0]
            next_hint = (
                f"{first.match_id}: "
                f"{tournament.player_name(first.p1)} vs "
                f"{tournament.player_name(first.p2)}"
            )

        champion = None
        if tournament.champion:
            player = tournament.get_player(tournament.champion)
            champion = {
                "name": player.name if player else tournament.champion,
                "seed": player.seed if player else "",
            }

        return {
            "name": tournament.name or "MAJOR 锦标赛",
            "size": tournament.size,
            "status_text": status_text,
            "player_count": tournament.player_count,
            "rounds": rounds,
            "champion": champion,
            "next_hint": next_hint,
            "created_at": tournament.created_at,
            "updated_at": tournament.updated_at,
        }

    def _match_context(self, tournament: Tournament, match: Match) -> dict[str, Any]:
        return {
            "id": match.match_id,
            "p1": self._side_context(tournament, match, match.p1),
            "p2": self._side_context(tournament, match, match.p2),
            "score1": "" if match.score1 is None else match.score1,
            "score2": "" if match.score2 is None else match.score2,
            "state": match.status,
            "finished": match.status == MATCH_FINISHED,
            "ready": match.status == MATCH_READY,
        }

    @staticmethod
    def _side_context(
        tournament: Tournament, match: Match, user_id: str | None
    ) -> dict[str, Any]:
        if not user_id:
            return {"name": "待定", "seed": "", "empty": True, "winner": False}
        player = tournament.get_player(user_id)
        return {
            "name": player.name if player else str(user_id),
            "seed": player.seed if player else "",
            "empty": False,
            "winner": match.winner == user_id,
        }

    # ────────── 纯文本 ──────────
    def render_text(self, tournament: Tournament) -> str:
        lines: list[str] = []
        title = tournament.name or "MAJOR 锦标赛"
        lines.append(f"🏆 {title} · {tournament.size}强单败淘汰赛")
        if tournament.status == STATUS_REGISTRATION:
            lines.append(f"状态：报名中（{tournament.player_count}/{tournament.size}）")
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
