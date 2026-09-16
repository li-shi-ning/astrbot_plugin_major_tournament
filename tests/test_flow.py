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


async def _create_room(plugin, uid="admin", name="管理员", admin=True, args=""):
    text = "major 创建房间" + (f" {args}" if args else "")
    return await _run(plugin, FakeEvent(uid, name, admin=admin, text=text))


def test_command_parser_strips_prefix():
    plugin = _make_plugin(Path("/tmp"))
    assert plugin._raw_args(FakeEvent(text="major 胜 R8-1 1")) == "胜 R8-1 1"
    assert plugin._raw_args(FakeEvent(text="锦标赛 报名")) == "报名"
    assert plugin._raw_args(FakeEvent(text="major")) == ""


def test_full_tournament_flow(tmp_path):
    plugin = _make_plugin(tmp_path)

    async def scenario():
        await _create_room(plugin, uid="admin", name="管理员", admin=True)
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


def test_non_host_cannot_judge(tmp_path):
    """非房主、非管理员不能判胜（u1 是创建者，改用 u2）。"""
    plugin = _make_plugin(tmp_path)

    async def scenario():
        await _create_room(plugin, uid="admin", name="管理员", admin=True)
        for i in range(1, 5):
            await _run(plugin, FakeEvent(f"u{i}", f"选手{i}", text="major 报名"))
        await _run(
            plugin, FakeEvent("admin", "管理员", admin=True, text="major 开赛 4")
        )

        results = await _run(
            plugin, FakeEvent("u2", "选手2", admin=False, text="major 胜 R4-1 1")
        )
        assert "房主" in results[0][1] or "管理员" in results[0][1]

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
        await _create_room(plugin, uid="admin", name="管理员", admin=True)
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


def test_rename_permission_and_persist(tmp_path):
    plugin = _make_plugin(tmp_path)

    async def scenario():
        await _create_room(plugin, uid="u1", name="选手1", admin=False)
        # u1 报名成房主
        await _run(plugin, FakeEvent("u1", "选手1", text="major 报名"))
        # u2 非房主非管理员 -> 不能改名
        denied = await _run(
            plugin, FakeEvent("u2", "选手2", admin=False, text="major 命名 我的杯")
        )
        assert "房主" in denied[0][1] or "管理员" in denied[0][1]

        # 管理员可以改名
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
        await _create_room(plugin, uid="admin", name="管理员", admin=True)
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
        await _create_room(
            plugin, uid="admin", name="管理员", admin=True, args="我的杯 4"
        )
        for i in range(1, 5):
            await _run(plugin, FakeEvent(f"u{i}", f"选手{i}", text="major 报名"))
        await _run(plugin, FakeEvent("admin", "管理员", admin=True, text="major 开赛"))
        tournament = plugin.store.load("g1", "testplat")
        assert tournament.name == "我的杯"
        assert tournament.size == 4

    asyncio.run(scenario())


def test_start_announces_draw_and_list_shows_seeds(tmp_path):
    plugin = _make_plugin(tmp_path)

    async def scenario():
        await _create_room(plugin, uid="admin", name="管理员", admin=True)
        for i in range(1, 9):
            await _run(plugin, FakeEvent(f"u{i}", f"选手{i}", text="major 报名"))

        start_results = await _run(
            plugin, FakeEvent("admin", "管理员", admin=True, text="major 开赛 8")
        )
        assert "随机抽签" in start_results[0][1]

        list_results = await _run(plugin, FakeEvent("u1", "选手1", text="major 名单"))
        assert "抽签种子" in list_results[0][1]

    asyncio.run(scenario())


def test_redraw_command(tmp_path):
    plugin = _make_plugin(tmp_path)

    async def scenario():
        await _create_room(plugin, uid="u1", name="选手1", admin=False)
        for i in range(1, 9):
            await _run(plugin, FakeEvent(f"u{i}", f"选手{i}", text="major 报名"))
        await _run(
            plugin, FakeEvent("admin", "管理员", admin=True, text="major 开赛 8")
        )

        # 非房主、非管理员不能重抽（u1 是房主，改用 u2）
        denied = await _run(plugin, FakeEvent("u2", "选手2", text="major 重抽"))
        assert "房主" in denied[0][1] or "管理员" in denied[0][1]

        # 管理员可以重抽
        allowed = await _run(
            plugin, FakeEvent("admin", "管理员", admin=True, text="major 重抽")
        )
        assert "重新抽签" in allowed[0][1]

    asyncio.run(scenario())


def test_host_without_admin_can_start(tmp_path):
    """房主不是管理员也能开赛。"""
    plugin = _make_plugin(tmp_path)

    async def scenario():
        await _create_room(plugin, uid="u1", name="选手1", admin=False)
        for i in range(1, 5):
            await _run(plugin, FakeEvent(f"u{i}", f"选手{i}", text="major 报名"))
        results = await _run(
            plugin, FakeEvent("u1", "选手1", admin=False, text="major 开赛 4")
        )
        assert "开赛" in results[0][1]
        assert plugin.store.load("g1", "testplat").status == "running"

    asyncio.run(scenario())


def test_create_room_name_and_optional_size(tmp_path):
    plugin = _make_plugin(tmp_path)

    async def scenario():
        results = await _run(
            plugin,
            FakeEvent("u1", "房主", admin=False, text="major 创建房间 我的杯 16"),
        )
        assert "房间已创建" in results[0][1]
        tournament = plugin.store.load("g1", "testplat")
        assert tournament.name == "我的杯"
        assert tournament.size == 16
        assert tournament.creator_id == "u1"

        # 已有房间时不能重复创建
        again = await _run(
            plugin, FakeEvent("u2", "路人", text="major 创建房间 别的杯")
        )
        assert "已有房间" in again[0][1]

        # 报名需要先有房间
        await _run(plugin, FakeEvent("u1", "房主", admin=False, text="major 重置"))
        join = await _run(plugin, FakeEvent("u2", "路人", text="major 报名"))
        assert "创建房间" in join[0][1]

        # 不带名称与规模：名称用默认，规模 0（开赛自动）
        created = await _run(plugin, FakeEvent("u2", "路人", text="major 创建房间"))
        assert "房间已创建" in created[0][1]
        tournament = plugin.store.load("g1", "testplat")
        assert tournament.name
        assert tournament.size == 0

    asyncio.run(scenario())


def test_room_size_is_used_at_start(tmp_path):
    """创建房间时指定的规模，开赛时沿用（人数不足则轮空）。"""
    plugin = _make_plugin(tmp_path)

    async def scenario():
        await _run(
            plugin,
            FakeEvent("admin", "管理员", admin=True, text="major 创建房间 固定杯 16"),
        )
        for i in range(1, 6):
            await _run(plugin, FakeEvent(f"u{i}", f"选手{i}", text="major 报名"))
        await _run(plugin, FakeEvent("admin", "管理员", admin=True, text="major 开赛"))
        tournament = plugin.store.load("g1", "testplat")
        assert tournament.size == 16

    asyncio.run(scenario())
