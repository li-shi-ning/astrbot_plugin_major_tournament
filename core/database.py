"""基于 SQLite 的赛事持久化。

设计：
- ``tournaments`` / ``players`` / ``matches`` 保存当前赛事及其完整赛程；
- ``match_history`` 保存每一场已判定比赛的结果（含胜者/败者/比分/时间），
  即使管理员重置赛事也不会丢失，便于「major 记录」查询；
- 每个群每个平台同一时间只有一场 active 赛事，重置只是把 active 置 0，
  历史数据保留。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import (
    Match,
    Player,
    Round,
    Tournament,
    new_tournament_id,
    now_iso,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tournaments (
    tournament_id TEXT PRIMARY KEY,
    group_id      TEXT NOT NULL,
    platform_id   TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    size          INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'registration',
    champion      TEXT,
    creator_id    TEXT NOT NULL DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_tournaments_group
    ON tournaments (platform_id, group_id, active);

CREATE TABLE IF NOT EXISTS players (
    tournament_id TEXT NOT NULL,
    user_id       TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    seed          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (tournament_id, user_id)
);

CREATE TABLE IF NOT EXISTS matches (
    tournament_id TEXT NOT NULL,
    match_id      TEXT NOT NULL,
    round_index   INTEGER NOT NULL DEFAULT 0,
    round_name    TEXT NOT NULL DEFAULT '',
    slot_index    INTEGER NOT NULL DEFAULT 0,
    p1            TEXT,
    p2            TEXT,
    p1_name       TEXT NOT NULL DEFAULT '',
    p2_name       TEXT NOT NULL DEFAULT '',
    score1        INTEGER,
    score2        INTEGER,
    winner        TEXT,
    winner_name   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    updated_at    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (tournament_id, match_id)
);

CREATE TABLE IF NOT EXISTS match_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tournament_id TEXT NOT NULL,
    group_id      TEXT NOT NULL,
    platform_id   TEXT NOT NULL DEFAULT '',
    match_id      TEXT NOT NULL,
    round_name    TEXT NOT NULL DEFAULT '',
    winner        TEXT,
    winner_name   TEXT NOT NULL DEFAULT '',
    loser         TEXT,
    loser_name    TEXT NOT NULL DEFAULT '',
    score1        INTEGER,
    score2        INTEGER,
    decided_at    TEXT NOT NULL DEFAULT '',
    UNIQUE (tournament_id, match_id)
);

CREATE INDEX IF NOT EXISTS idx_history_group
    ON match_history (platform_id, group_id, id);
"""


class TournamentDatabase:
    """SQLite 存储，接口与旧的 TournamentStore 保持一致。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._migrate_schema()
            self._conn.commit()

    def _migrate_schema(self) -> None:
        """为旧版本数据库补齐新增列（CREATE TABLE IF NOT EXISTS 不会补列）。"""
        columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(tournaments)")
        }
        if "creator_id" not in columns:
            self._conn.execute(
                "ALTER TABLE tournaments ADD COLUMN creator_id TEXT NOT NULL DEFAULT ''"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ─────────────────── 读取 ───────────────────
    def load(self, group_id: str, platform_id: str = "") -> Tournament | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM tournaments
                WHERE group_id = ? AND platform_id = ? AND active = 1
                ORDER BY updated_at DESC LIMIT 1
                """,
                (str(group_id), str(platform_id)),
            ).fetchone()
            return self._row_to_tournament(row) if row is not None else None

    def load_by_id(self, tournament_id: str) -> Tournament | None:
        """按 tournament_id 读取任意一场赛事（包含已结束/已重置的历史赛事）。"""
        tid = str(tournament_id or "").strip()
        if not tid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tournaments WHERE tournament_id = ?", (tid,)
            ).fetchone()
            return self._row_to_tournament(row) if row is not None else None

    def count_tournaments(self, group_id: str, platform_id: str = "") -> int:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS total FROM tournaments
                WHERE group_id = ? AND platform_id = ?
                """,
                (str(group_id), str(platform_id)),
            ).fetchone()
        return int(row["total"]) if row else 0

    def list_tournaments(
        self,
        group_id: str,
        platform_id: str = "",
        limit: int = 5,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按时间倒序列出该群的赛事（含已结束的）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    t.tournament_id, t.name, t.size, t.status, t.champion,
                    t.creator_id, t.created_at, t.updated_at, t.active,
                    (SELECT COUNT(*) FROM players p
                     WHERE p.tournament_id = t.tournament_id) AS player_count,
                    (SELECT p.name FROM players p
                     WHERE p.tournament_id = t.tournament_id
                       AND p.user_id = t.champion LIMIT 1) AS champion_name
                FROM tournaments t
                WHERE t.group_id = ? AND t.platform_id = ?
                ORDER BY t.created_at DESC, t.rowid DESC
                LIMIT ? OFFSET ?
                """,
                (
                    str(group_id),
                    str(platform_id),
                    max(1, int(limit)),
                    max(0, int(offset)),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def rank_of(self, tournament_id: str, group_id: str, platform_id: str = "") -> int:
        """返回该赛事在列表中的序号（1-based，最近的一场为 1）。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT tournament_id FROM tournaments
                WHERE group_id = ? AND platform_id = ?
                ORDER BY created_at DESC, rowid DESC
                """,
                (str(group_id), str(platform_id)),
            ).fetchall()
        ids = [row["tournament_id"] for row in rows]
        tid = str(tournament_id or "")
        return ids.index(tid) + 1 if tid in ids else 0

    def tournament_at_rank(
        self, group_id: str, platform_id: str, rank: int
    ) -> Tournament | None:
        """按 1-based 序号读取赛事。"""
        index = max(0, int(rank) - 1)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM tournaments
                WHERE group_id = ? AND platform_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1 OFFSET ?
                """,
                (str(group_id), str(platform_id), index),
            ).fetchone()
            return self._row_to_tournament(row) if row is not None else None

    def _row_to_tournament(self, row: sqlite3.Row) -> Tournament:
        tournament = Tournament(
            group_id=row["group_id"],
            platform_id=row["platform_id"],
            name=row["name"],
            size=int(row["size"]),
            status=row["status"],
            champion=row["champion"],
            creator_id=row["creator_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            tournament_id=row["tournament_id"],
        )

        player_rows = self._conn.execute(
            "SELECT user_id, name, seed FROM players WHERE tournament_id = ?",
            (tournament.tournament_id,),
        ).fetchall()
        tournament.players = [
            Player(
                user_id=item["user_id"],
                name=item["name"],
                seed=int(item["seed"]),
            )
            for item in player_rows
        ]

        match_rows = self._conn.execute(
            """
            SELECT * FROM matches WHERE tournament_id = ?
            ORDER BY round_index ASC, slot_index ASC
            """,
            (tournament.tournament_id,),
        ).fetchall()
        tournament.rounds = self._rebuild_rounds(match_rows, tournament.players)
        return tournament

    @staticmethod
    def _rebuild_rounds(
        match_rows: list[sqlite3.Row], players: list[Player]
    ) -> list[Round]:
        rounds: list[Round] = []
        current_index = -1
        for row in match_rows:
            round_index = int(row["round_index"])
            if round_index != current_index:
                rounds.append(Round(name=row["round_name"], matches=[]))
                current_index = round_index
            rounds[-1].matches.append(
                Match(
                    match_id=row["match_id"],
                    round_index=round_index,
                    index=int(row["slot_index"]),
                    p1=row["p1"],
                    p2=row["p2"],
                    score1=row["score1"],
                    score2=row["score2"],
                    winner=row["winner"],
                    status=row["status"],
                )
            )
        return rounds

    def list_groups(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT group_id FROM tournaments WHERE active = 1"
            ).fetchall()
        return [row["group_id"] for row in rows]

    def recent_history(
        self, group_id: str, platform_id: str = "", limit: int = 10
    ) -> list[dict[str, Any]]:
        """返回最近判定过的比赛结果。"""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM match_history
                WHERE group_id = ? AND platform_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (str(group_id), str(platform_id), max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in rows]

    def tournament_history(self, tournament_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM match_history WHERE tournament_id = ? ORDER BY id ASC",
                (str(tournament_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    # ─────────────────── 写入 ───────────────────
    def save(self, tournament: Tournament) -> None:
        tournament.touch()
        if not tournament.tournament_id:
            tournament.tournament_id = new_tournament_id()

        with self._lock:
            cursor = self._conn.cursor()
            # 同群只保留一场 active 赛事
            cursor.execute(
                """
                UPDATE tournaments SET active = 0
                WHERE group_id = ? AND platform_id = ? AND tournament_id <> ?
                """,
                (tournament.group_id, tournament.platform_id, tournament.tournament_id),
            )
            cursor.execute(
                """
                INSERT INTO tournaments (
                    tournament_id, group_id, platform_id, name, size, status,
                    champion, creator_id, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(tournament_id) DO UPDATE SET
                    group_id = excluded.group_id,
                    platform_id = excluded.platform_id,
                    name = excluded.name,
                    size = excluded.size,
                    status = excluded.status,
                    champion = excluded.champion,
                    creator_id = excluded.creator_id,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    tournament.tournament_id,
                    tournament.group_id,
                    tournament.platform_id,
                    tournament.name,
                    int(tournament.size),
                    tournament.status,
                    tournament.champion,
                    tournament.creator_id,
                    tournament.created_at,
                    tournament.updated_at,
                ),
            )

            cursor.execute(
                "DELETE FROM players WHERE tournament_id = ?",
                (tournament.tournament_id,),
            )
            cursor.executemany(
                """
                INSERT INTO players (tournament_id, user_id, name, seed)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (tournament.tournament_id, p.user_id, p.name, int(p.seed))
                    for p in tournament.players
                ],
            )

            cursor.execute(
                "DELETE FROM matches WHERE tournament_id = ?",
                (tournament.tournament_id,),
            )
            for round_ in tournament.rounds:
                for match in round_.matches:
                    cursor.execute(
                        """
                        INSERT INTO matches (
                            tournament_id, match_id, round_index, round_name,
                            slot_index, p1, p2, p1_name, p2_name,
                            score1, score2, winner, winner_name, status, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            tournament.tournament_id,
                            match.match_id,
                            int(match.round_index),
                            round_.name,
                            int(match.index),
                            match.p1,
                            match.p2,
                            tournament.player_name(match.p1) if match.p1 else "",
                            tournament.player_name(match.p2) if match.p2 else "",
                            match.score1,
                            match.score2,
                            match.winner,
                            tournament.player_name(match.winner)
                            if match.winner
                            else "",
                            match.status,
                            tournament.updated_at,
                        ),
                    )
                    # 只记录真实对局（双方都有人的比赛），轮空不计入历史
                    if match.winner and match.p1 and match.p2:
                        self._record_history(cursor, tournament, round_.name, match)

            self._conn.commit()

    @staticmethod
    def _record_history(
        cursor: sqlite3.Cursor,
        tournament: Tournament,
        round_name: str,
        match: Match,
    ) -> None:
        if not match.winner:
            return
        loser = match.p2 if match.winner == match.p1 else match.p1
        cursor.execute(
            """
            INSERT OR IGNORE INTO match_history (
                tournament_id, group_id, platform_id, match_id, round_name,
                winner, winner_name, loser, loser_name,
                score1, score2, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tournament.tournament_id,
                tournament.group_id,
                tournament.platform_id,
                match.match_id,
                round_name,
                match.winner,
                tournament.player_name(match.winner),
                loser,
                tournament.player_name(loser) if loser else "",
                match.score1,
                match.score2,
                now_iso(),
            ),
        )

    def delete(self, group_id: str, platform_id: str = "") -> bool:
        """结束当前赛事：置为非 active，但保留历史记录。"""
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE tournaments SET active = 0, updated_at = ?
                WHERE group_id = ? AND platform_id = ? AND active = 1
                """,
                (now_iso(), str(group_id), str(platform_id)),
            )
            self._conn.commit()
            return cursor.rowcount > 0
