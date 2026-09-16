"""赛程生成 / 晋级逻辑单测（不依赖 AstrBot）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import bracket as bk  # noqa: E402
from core.models import (  # noqa: E402
    MATCH_READY,
    STATUS_FINISHED,
    STATUS_RUNNING,
    Tournament,
)


def make_tournament(count: int, size: int = 32) -> Tournament:
    t = Tournament(group_id="g1", size=size)
    for i in range(count):
        t.add_player(f"u{i + 1}", f"P{i + 1}")
    return t


def test_seed_order_is_standard():
    assert bk.seed_order(2) == [1, 2]
    assert bk.seed_order(4) == [1, 4, 2, 3]
    assert bk.seed_order(8) == [1, 8, 4, 5, 2, 7, 3, 6]


def test_next_power_of_two_and_normalize():
    assert bk.next_power_of_two(1) == 2
    assert bk.next_power_of_two(5) == 8
    assert bk.next_power_of_two(32) == 32
    assert bk.normalize_size(None, 5) == 8
    assert bk.normalize_size(32, 5) == 32
    # 指定规模装不下报名人数时自动放大
    assert bk.normalize_size(4, 9) == 16
    # 非法规模自动推导
    assert bk.normalize_size(7, 6) == 8


def test_start_creates_all_rounds():
    t = make_tournament(32)
    bk.start_tournament(t, size=32, seed_mode="register")

    assert t.status == STATUS_RUNNING
    assert [r.name for r in t.rounds] == [
        "32强",
        "16强",
        "8强",
        "4强",
        "决赛",
    ]
    assert [len(r.matches) for r in t.rounds] == [16, 8, 4, 2, 1]


def test_register_seeding_pairs_by_seed_order():
    t = make_tournament(8)
    bk.start_tournament(t, size=8, seed_mode="register")
    first = t.rounds[0].matches

    pairs = [(m.p1, m.p2) for m in first]
    assert pairs == [
        ("u1", "u8"),
        ("u4", "u5"),
        ("u2", "u7"),
        ("u3", "u6"),
    ]
    assert all(m.status == MATCH_READY for m in first)


def test_manual_winner_advances_to_next_round_and_crowns_champion():
    t = make_tournament(4)
    bk.start_tournament(t, size=4, seed_mode="register")
    # order [1,4,2,3] -> (u1 vs u4), (u2 vs u3)
    ok, _ = bk.set_winner(t, "R4-1", "1")
    assert ok
    ok, _ = bk.set_winner(t, "R4-2", "2")  # u3 胜出
    assert ok

    final = t.rounds[1].matches[0]
    assert final.p1 == "u1"
    assert final.p2 == "u3"
    assert final.status == MATCH_READY

    ok, msg = bk.set_winner(t, "R2-1", "1")
    assert ok
    assert t.status == STATUS_FINISHED
    assert t.champion == "u1"
    assert "冠军" in msg


def test_byes_auto_advance():
    t = make_tournament(5, size=8)
    # 报名顺序即种子 1..5；种子 6/7/8 轮空
    bk.start_tournament(t, size=8, seed_mode="register")
    second = t.rounds[1].matches
    # 种子 1、2、3 轮空进入 8 强的下一轮（4强）
    advanced = {m.p1 for m in second} | {m.p2 for m in second}
    assert {"u1", "u2", "u3"} <= advanced


def test_cannot_judge_unready_or_rejudge():
    t = make_tournament(8)
    bk.start_tournament(t, size=8, seed_mode="register")
    ok, msg = bk.set_winner(t, "R8-1", "1")
    assert ok
    ok, msg = bk.set_winner(t, "R8-1", "2")
    assert not ok and "已经判定" in msg
    ok, msg = bk.set_winner(t, "NOPE", "1")
    assert not ok and "找不到" in msg


def test_resolve_winner_by_name():
    t = make_tournament(4)
    bk.start_tournament(t, size=4, seed_mode="register")
    ok, msg = bk.set_winner(t, "R4-1", "P4")
    assert ok
    assert t.rounds[0].matches[0].winner == "u4"
