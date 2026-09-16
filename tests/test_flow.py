"""插件命令层集成测试。

依赖 AstrBot 运行时；未安装 astrbot 时自动跳过。
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytest.importorskip("astrbot", reason="需要 AstrBot 运行环境")

from astrbot_plugin_major_tournament.core.bracket import pending_matches  # noqa: E402
from astrbot_plugin_major_tournament.core.database import (  # noqa: E402
    TournamentDatabase,
)
from astrbot_plugin_major_tournament.main import MajorTournament  # noqa: E402


class FakeEvent:
    def __init__(self, uid="u1", name="选手", group="g1", admin=False, text=""):
        self.uid = uid
        self.name = name
        self.group = group
        self.admin = admin
        self.text = text

    def get_message_str(self):
        return self.text

    def get_group_id(self):
        return self.group

    def get_platform_id(self):
        return "testplat"

    def get_sender_id(self):
        return self.uid

    def get_sender_name(self):
        return self.name

    def is_admin(self):
        return self.admin

    def should_call_llm(self, *_):
        return None

    def get_messages(self):
        return []

    def plain_result(self, text):
        return ("text", text)

    def image_result(self, path_or_url):
        return ("image", path_or_url)

    def make_result(self):
        return self

    def base64_image(self, b64):
        return ("b64image", b64)


async def _run(plugin, event):
    return [item async for item in plugin.major(event)]


def _make_plugin(tmp_path):
    plugin = MajorTournament(None, {})
    plugin.store = TournamentDatabase(tmp_path / "test.db")
    return plugin


def test_command_parser_strips_prefix():
    plugin = _make_plugin(Path("/tmp"))
    assert plugin._raw_args(FakeEvent(text="major 胜 R8-1 1")) == "胜 R8-1 1"
    assert plugin._raw_args(FakeEvent(text="锦标赛 报名")) == "报名"
    assert plugin._raw_args(FakeEvent(text="major")) == ""


def test_full_tournament_flow(tmp_path):
    plugin = _make_plugin(tmp_path)

    async def scenario():
        # 1. 报名 8 人
        for i in range(1, 9):
            results = await _run(
                plugin, FakeEvent(f"u{i}", f"选手{i}", text="major 报名")
            )
            assert "报名成功" in results[0][1]

        # 2. 开赛（自动取 8 强）
        results = await _run(
            plugin,
            FakeEvent("admin", "管理员", admin=True, text="major 开赛 8"),
        )
        assert "开赛" in results[0][1]

        tournament = plugin.store.load("g1", "testplat")
        assert tournament is not None
        assert tournament.size == 8
        assert [r.name for r in tournament.rounds] == ["8强", "4强", "决赛"]

        # 3. 逐场判胜，直到产生冠军
        guard = 0
        while tournament.status != "finished" and guard < 20:
            guard += 1
            matches = pending_matches(tournament)
            assert matches, "进行中应始终有可判定的比赛"
            match = matches[0]
            results = await _run(
                plugin,
                FakeEvent(
                    "admin",
                    "管理员",
                    admin=True,
                    text=f"major 胜 {match.match_id} 1",
                ),
            )
            assert results[0][1].startswith("✅") or "冠军" in results[0][1]
            tournament = plugin.store.load("g1", "testplat")

        assert tournament.status == "finished"
        assert tournament.champion

    asyncio.run(scenario())


def test_non_admin_cannot_judge(tmp_path):
    plugin = _make_plugin(tmp_path)

    async def scenario():
        for i in range(1, 5):
            await _run(plugin, FakeEvent(f"u{i}", f"选手{i}", text="major 报名"))
        await _run(
            plugin, FakeEvent("admin", "管理员", admin=True, text="major 开赛 4")
        )

        results = await _run(
            plugin, FakeEvent("u1", "选手1", admin=False, text="major 胜 R4-1 1")
        )
        assert "管理员" in results[0][1]

    asyncio.run(scenario())


def test_yield_image_handles_bytes_url_and_file(tmp_path):
    plugin = _make_plugin(tmp_path)
    event = FakeEvent()

    async def passthrough(result):
        return [item async for item in plugin._yield_bracket_image(event, result)]

    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    results = asyncio.run(passthrough(png.read_bytes()))
    assert results[0][0] == "b64image"

    results = asyncio.run(passthrough("https://example.com/a.png"))
    assert results[0][0] == "image"

    results = asyncio.run(passthrough(str(png)))
    assert results[0][0] == "b64image"

    results = asyncio.run(passthrough(None))
    assert results[0][0] == "text"


def test_registration_not_capped_and_auto_size(tmp_path):
    """默认配置下报名不应被 32 卡住，开赛时按人数自动定规模。"""
    plugin = _make_plugin(tmp_path)

    async def scenario():
        for i in range(1, 34):  # 报名 33 人
            results = await _run(
                plugin, FakeEvent(f"u{i}", f"选手{i}", text="major 报名")
            )
            assert "报名成功" in results[0][1], results

        tournament = plugin.store.load("g1", "testplat")
        assert tournament.player_count == 33
        assert tournament.size == 0  # 报名阶段规模未定

        results = await _run(
            plugin, FakeEvent("admin", "管理员", admin=True, text="major 开赛")
        )
        assert "64 强" in results[0][1]

        tournament = plugin.store.load("g1", "testplat")
        assert tournament.size == 64
        assert len(tournament.rounds[0].matches) == 32

    asyncio.run(scenario())


def test_rename_requires_admin_and_persists(tmp_path):
    plugin = _make_plugin(tmp_path)

    async def scenario():
        results = await _run(
            plugin,
            FakeEvent("u1", "选手1", admin=False, text="major 命名 我的杯"),
        )
        assert "管理员" in results[0][1]

        results = await _run(
            plugin,
            FakeEvent("admin", "管理员", admin=True, text="major 命名 群友 MAJOR 杯"),
        )
        assert "群友 MAJOR 杯" in results[0][1]
        assert plugin.store.load("g1", "testplat").name == "群友 MAJOR 杯"

    asyncio.run(scenario())


def test_history_command_reads_database(tmp_path):
    plugin = _make_plugin(tmp_path)

    async def scenario():
        for i in range(1, 5):
            await _run(plugin, FakeEvent(f"u{i}", f"选手{i}", text="major 报名"))
        await _run(
            plugin, FakeEvent("admin", "管理员", admin=True, text="major 开赛 4")
        )
        tournament = plugin.store.load("g1", "testplat")
        match = pending_matches(tournament)[0]
        await _run(
            plugin,
            FakeEvent(
                "admin",
                "管理员",
                admin=True,
                text=f"major 胜 {match.match_id} 1 2:1",
            ),
        )

        results = await _run(plugin, FakeEvent("u1", "选手1", text="major 记录"))
        text = results[0][1]
        assert "比赛记录" in text
        assert match.match_id in text
        assert "2:1" in text

    asyncio.run(scenario())


def test_open_with_custom_name(tmp_path):
    plugin = _make_plugin(tmp_path)

    async def scenario():
        for i in range(1, 5):
            await _run(plugin, FakeEvent(f"u{i}", f"选手{i}", text="major 报名"))
        await _run(
            plugin,
            FakeEvent("admin", "管理员", admin=True, text="major 开赛 4 我的杯"),
        )
        tournament = plugin.store.load("g1", "testplat")
        assert tournament.name == "我的杯"
        assert tournament.size == 4

    asyncio.run(scenario())
