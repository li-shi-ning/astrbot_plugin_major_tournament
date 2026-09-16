"""Major 赛制插件的领域模型。

只依赖标准库，方便单测与序列化。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    """返回当前本地时间的 ISO 字符串（精确到秒）。"""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_tournament_id() -> str:
    """生成一个短且唯一的赛事 ID（用于数据库主键 / 历史归档）。"""
    return uuid.uuid4().hex[:12]


#: 赛事状态
STATUS_REGISTRATION = "registration"
STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"

#: 比赛状态
MATCH_PENDING = "pending"  # 两个位置都空
MATCH_WAITING = "waiting"  # 只有一个位置有人，等待上一轮胜者
MATCH_READY = "ready"  # 两人到齐，可以开打
MATCH_FINISHED = "finished"  # 已判定胜者


@dataclass
class Player:
    """一名报名参赛的选手。"""

    user_id: str
    name: str
    seed: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Player:
        return cls(
            user_id=str(data.get("user_id", "")),
            name=str(data.get("name", "") or data.get("user_id", "")),
            seed=int(data.get("seed", 0) or 0),
        )


@dataclass
class Match:
    """一场单败淘汰赛。p1/p2/winner 存的是选手的 user_id。"""

    match_id: str
    round_index: int
    index: int
    p1: str | None = None
    p2: str | None = None
    score1: int | None = None
    score2: int | None = None
    winner: str | None = None
    status: str = MATCH_PENDING

    @property
    def is_finished(self) -> bool:
        return self.status == MATCH_FINISHED

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Match:
        return cls(
            match_id=str(data.get("match_id", "")),
            round_index=int(data.get("round_index", 0) or 0),
            index=int(data.get("index", 0) or 0),
            p1=data.get("p1"),
            p2=data.get("p2"),
            score1=data.get("score1"),
            score2=data.get("score2"),
            winner=data.get("winner"),
            status=str(data.get("status", MATCH_PENDING)),
        )


@dataclass
class Round:
    """一个轮次（例如 32 强、16 强……决赛）。"""

    name: str
    matches: list[Match] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "matches": [m.to_dict() for m in self.matches],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Round:
        return cls(
            name=str(data.get("name", "")),
            matches=[Match.from_dict(m) for m in data.get("matches", [])],
        )


@dataclass
class Tournament:
    """一场群内 Major 赛事。"""

    group_id: str
    platform_id: str = ""
    name: str = ""
    #: 赛事规模（2/4/8/16/32/64）。0 表示报名阶段不限人数，开赛时自动确定。
    size: int = 0
    status: str = STATUS_REGISTRATION
    players: list[Player] = field(default_factory=list)
    rounds: list[Round] = field(default_factory=list)
    champion: str | None = None
    message_id: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    #: 唯一赛事 ID，用于数据库记录与历史归档
    tournament_id: str = field(default_factory=new_tournament_id)

    # ────────── 查询 ──────────
    def get_player(self, user_id: str | None) -> Player | None:
        if not user_id:
            return None
        for player in self.players:
            if player.user_id == str(user_id):
                return player
        return None

    def player_name(self, user_id: str | None) -> str:
        if not user_id:
            return "待定"
        player = self.get_player(user_id)
        return player.name if player else str(user_id)

    @property
    def player_count(self) -> int:
        return len(self.players)

    @property
    def is_registration_open(self) -> bool:
        return self.status == STATUS_REGISTRATION

    # ────────── 报名 ──────────
    def find_player_index(self, user_id: str) -> int:
        for index, player in enumerate(self.players):
            if player.user_id == str(user_id):
                return index
        return -1

    def add_player(self, user_id: str, name: str = "") -> bool:
        """报名。已报名或人数已满时返回 False。"""
        user_id = str(user_id or "").strip()
        if not user_id or self.find_player_index(user_id) >= 0:
            return False
        # size <= 0 表示报名阶段不限制人数，开赛时再自动确定规模
        if self.size > 0 and len(self.players) >= self.size:
            return False
        self.players.append(
            Player(user_id=user_id, name=str(name or user_id).strip(), seed=0)
        )
        self.touch()
        return True

    def remove_player(self, user_id: str) -> bool:
        index = self.find_player_index(user_id)
        if index < 0:
            return False
        self.players.pop(index)
        self.touch()
        return True

    def touch(self) -> None:
        self.updated_at = now_iso()

    # ────────── 序列化 ──────────
    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "platform_id": self.platform_id,
            "name": self.name,
            "size": self.size,
            "status": self.status,
            "players": [p.to_dict() for p in self.players],
            "rounds": [r.to_dict() for r in self.rounds],
            "champion": self.champion,
            "message_id": self.message_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tournament_id": self.tournament_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Tournament:
        return cls(
            group_id=str(data.get("group_id", "")),
            platform_id=str(data.get("platform_id", "")),
            name=str(data.get("name", "")),
            size=int(data.get("size", 0) or 0),
            status=str(data.get("status", STATUS_REGISTRATION)),
            players=[Player.from_dict(p) for p in data.get("players", [])],
            rounds=[Round.from_dict(r) for r in data.get("rounds", [])],
            champion=data.get("champion"),
            message_id=str(data.get("message_id", "")),
            created_at=str(data.get("created_at", now_iso())),
            updated_at=str(data.get("updated_at", now_iso())),
            tournament_id=str(data.get("tournament_id") or new_tournament_id()),
        )
