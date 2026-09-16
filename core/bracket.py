"""单败淘汰赛的对阵生成与晋级逻辑。

支持 2/4/8/16/32 强，采用标准种子排位，保证高顺位种子在早期
不会相遇，并自动把轮空（bye）选手送入下一轮。
"""

from __future__ import annotations

import random
from collections.abc import Iterable

from .models import (
    MATCH_FINISHED,
    MATCH_PENDING,
    MATCH_READY,
    MATCH_WAITING,
    STATUS_FINISHED,
    STATUS_REGISTRATION,
    STATUS_RUNNING,
    Match,
    Player,
    Round,
    Tournament,
)

#: 允许的赛事规模（强数）
VALID_SIZES: tuple[int, ...] = (2, 4, 8, 16, 32)

#: 各轮次中文名
ROUND_NAMES: dict[int, str] = {
    2: "决赛",
    4: "4强",
    8: "8强",
    16: "16强",
    32: "32强",
}


def next_power_of_two(count: int) -> int:
    """返回不小于 count 的 2 的幂（至少 2）。"""
    size = 2
    while size < count:
        size *= 2
    return size


def normalize_size(size: int | None, player_count: int) -> int:
    """把用户传入的规模规整为合法的 2 的幂。

    - 非法/未提供时，自动取“不小于报名人数的最小 2 的幂”。
    - 若指定规模小于报名人数，则放大到能容纳所有报名者的规模。
    """
    if size is None:
        return next_power_of_two(max(2, player_count))
    if size in VALID_SIZES:
        return max(size, next_power_of_two(max(2, player_count)))
    return next_power_of_two(max(2, player_count))


def seed_order(size: int) -> list[int]:
    """生成标准淘汰赛种子排位（1-based 种子号按位置排列）。

    例如 size=8 -> [1, 8, 4, 5, 2, 7, 3, 6]，
    相邻两个位置组成首轮对阵：(1 vs 8), (4 vs 5), (2 vs 7), (3 vs 6)。
    """
    if size < 2 or size & (size - 1):
        raise ValueError(f"size 必须是 2 的幂，收到 {size}")
    order = [1, 2]
    while len(order) < size:
        length = len(order) * 2
        nxt: list[int] = []
        for seed in order:
            nxt.append(seed)
            nxt.append(length + 1 - seed)
        order = nxt
    return order


def round_name(players_in_round: int) -> str:
    return ROUND_NAMES.get(players_in_round, f"{players_in_round}强")


def _assign_seeds(players: list[Player], seed_mode: str) -> list[Player]:
    """给选手分配种子。随机种子优先保证公平，报名顺序用于复现。"""
    ordered = list(players)
    if seed_mode == "register":
        pass
    else:  # random（默认）
        random.shuffle(ordered)
    for index, player in enumerate(ordered, start=1):
        player.seed = index
    return ordered


def refresh_match(match: Match) -> None:
    """根据双方是否到齐刷新比赛状态。已判定的比赛不覆盖。"""
    if match.status == MATCH_FINISHED:
        return
    if match.p1 and match.p2:
        match.status = MATCH_READY
    elif match.p1 or match.p2:
        match.status = MATCH_WAITING
    else:
        match.status = MATCH_PENDING


def build_rounds(size: int) -> list[Round]:
    """按规模创建空的轮次结构。"""
    rounds: list[Round] = []
    players_in_round = size
    round_index = 0
    while players_in_round >= 2:
        matches = [
            Match(
                match_id=f"R{players_in_round}-{i + 1}",
                round_index=round_index,
                index=i,
            )
            for i in range(players_in_round // 2)
        ]
        rounds.append(Round(name=round_name(players_in_round), matches=matches))
        players_in_round //= 2
        round_index += 1
    return rounds


def _advance(tournament: Tournament, match: Match, winner_id: str) -> None:
    """把胜者写入下一轮；若已是决赛则产生冠军。"""
    next_round_index = match.round_index + 1
    if next_round_index >= len(tournament.rounds):
        tournament.champion = winner_id
        tournament.status = STATUS_FINISHED
        return
    next_match = tournament.rounds[next_round_index].matches[match.index // 2]
    if match.index % 2 == 0:
        next_match.p1 = winner_id
    else:
        next_match.p2 = winner_id
    refresh_match(next_match)


def _auto_advance_byes(tournament: Tournament) -> None:
    """自动让轮空选手晋级，直到没有新的轮空。"""
    while True:
        advanced = False
        for round_ in tournament.rounds:
            for match in round_.matches:
                if match.status == MATCH_FINISHED:
                    continue
                if bool(match.p1) ^ bool(match.p2):  # 恰好一人
                    winner_id = match.p1 or match.p2
                    match.winner = winner_id
                    match.status = MATCH_FINISHED
                    _advance(tournament, match, winner_id)  # type: ignore[arg-type]
                    advanced = True
        if not advanced:
            break


def start_tournament(
    tournament: Tournament,
    size: int | None = None,
    seed_mode: str = "random",
) -> None:
    """开始比赛：分配规模、种子，生成对阵并处理轮空。"""
    size = normalize_size(size, len(tournament.players))
    tournament.size = size
    tournament.status = STATUS_RUNNING
    tournament.champion = None
    tournament.rounds = build_rounds(size)

    ordered = _assign_seeds(tournament.players, seed_mode)
    order = seed_order(size)
    by_seed: dict[int, str] = {p.seed: p.user_id for p in ordered}

    first_round = tournament.rounds[0]
    for i, match in enumerate(first_round.matches):
        match.p1 = by_seed.get(order[2 * i])
        match.p2 = by_seed.get(order[2 * i + 1])
        refresh_match(match)

    _auto_advance_byes(tournament)
    tournament.touch()


def find_match(tournament: Tournament, match_id: str) -> Match | None:
    target = str(match_id or "").strip().upper()
    for round_ in tournament.rounds:
        for match in round_.matches:
            if match.match_id.upper() == target or match.match_id.upper().replace(
                "-", ""
            ) == target.replace("-", ""):
                return match
    return None


def resolve_winner_key(match: Match, key: str, tournament: Tournament) -> str | None:
    """把用户输入解析成胜者 user_id。

    key 可以是 "1"/"2"（位置）、选手昵称或 user_id。
    """
    text = str(key or "").strip()
    if not text:
        return None
    if text == "1":
        return match.p1
    if text == "2":
        return match.p2
    for user_id in (match.p1, match.p2):
        if not user_id:
            continue
        if text == user_id:
            return user_id
        player = tournament.get_player(user_id)
        if player and text == player.name:
            return user_id
    return None


def set_winner(
    tournament: Tournament,
    match_id: str,
    winner_key: str,
    score1: int | None = None,
    score2: int | None = None,
) -> tuple[bool, str]:
    """人工判定一场比赛的胜者，并自动把胜者送进下一轮。

    Returns:
        (是否成功, 提示信息)
    """
    if tournament.status == STATUS_REGISTRATION:
        return False, "赛事尚未开始，请先使用「major 开赛」。"
    match = find_match(tournament, match_id)
    if match is None:
        return False, f"找不到比赛「{match_id}」。可用「major 对阵」查看编号。"
    if match.status == MATCH_FINISHED:
        return False, f"比赛「{match.match_id}」已经判定过了。"
    if not match.p1 or not match.p2:
        return False, f"比赛「{match.match_id}」双方尚未到齐，无法判定。"

    winner_id = resolve_winner_key(match, winner_key, tournament)
    if not winner_id:
        return False, f"无法把「{winner_key}」解析为「{match.match_id}」的参赛选手。"

    match.winner = winner_id
    match.status = MATCH_FINISHED
    if winner_id == match.p1:
        match.score1 = score1 if score1 is not None else match.score1
        match.score2 = score2 if score2 is not None else match.score2
    else:
        match.score2 = score1 if score1 is not None else match.score2
        match.score1 = score2 if score2 is not None else match.score1

    _advance(tournament, match, winner_id)
    tournament.touch()

    if tournament.champion:
        return True, f"🏆 冠军诞生：{tournament.player_name(winner_id)}！"
    return True, f"已判定「{match.match_id}」胜者：{tournament.player_name(winner_id)}"


def pending_matches(tournament: Tournament) -> list[Match]:
    """返回所有双方到齐、等待人工判定的比赛。"""
    result: list[Match] = []
    for round_ in tournament.rounds:
        for match in round_.matches:
            if match.status == MATCH_READY:
                result.append(match)
    return result


def iter_players(tournament: Tournament) -> Iterable[Player]:
    return tournament.players
