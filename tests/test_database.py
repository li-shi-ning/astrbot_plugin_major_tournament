"""SQLite 存储层测试（不依赖 AstrBot）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_major_tournament.core.bracket import (  # noqa: E402
    pending_matches,
    set_winner,
    start_tournament,
)
from astrbot_plugin_major_tournament.core.database import (  # noqa: E402
    TournamentDatabase,
)
from astrbot_plugin_major_tournament.core.models import Tournament  # noqa: E402


def make_db(tmp_path) -> TournamentDatabase:
    return TournamentDatabase(tmp_path / "major.db")


def test_save_and_load_roundtrip(tmp_path):
    db = make_db(tmp_path)
    t = Tournament(group_id="g1", platform_id="p1", name="群友杯")
    for i in range(4):
        t.add_player(f"u{i + 1}", f"P{i + 1}")
    db.save(t)

    loaded = db.load("g1", "p1")
    assert loaded is not None
    assert loaded.name == "群友杯"
    assert loaded.tournament_id == t.tournament_id
    assert [p.name for p in loaded.players] == ["P1", "P2", "P3", "P4"]


def test_rename_persists_across_reload(tmp_path):
    db = make_db(tmp_path)
    t = Tournament(group_id="g1", platform_id="p1")
    db.save(t)
    loaded = db.load("g1", "p1")
    loaded.name = "新名字"
    db.save(loaded)

    assert db.load("g1", "p1").name == "新名字"


def test_history_recorded_and_kept_after_reset(tmp_path):
    db = make_db(tmp_path)
    t = Tournament(group_id="g1", platform_id="p1", name="杯")
    for i in range(4):
        t.add_player(f"u{i + 1}", f"P{i + 1}")
    db.save(t)

    loaded = db.load("g1", "p1")
    start_tournament(loaded, size=4, seed_mode="register")
    match = pending_matches(loaded)[0]
    set_winner(loaded, match.match_id, "1", score1=2, score2=0)
    db.save(loaded)

    history = db.recent_history("g1", "p1")
    assert len(history) == 1
    assert history[0]["match_id"] == match.match_id
    assert history[0]["score1"] == 2
    assert history[0]["score2"] == 0

    # 重置：当前赛事消失，但历史仍可查
    assert db.delete("g1", "p1") is True
    assert db.load("g1", "p1") is None
    assert len(db.recent_history("g1", "p1")) == 1


def test_new_tournament_gets_new_id_and_history_isolated(tmp_path):
    db = make_db(tmp_path)
    first = Tournament(group_id="g1", platform_id="p1")
    db.save(first)
    db.delete("g1", "p1")

    second = Tournament(group_id="g1", platform_id="p1")
    db.save(second)
    assert second.tournament_id != first.tournament_id
    assert db.load("g1", "p1").tournament_id == second.tournament_id
