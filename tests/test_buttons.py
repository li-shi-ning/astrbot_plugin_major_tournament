"""QQ 官方按钮面板测试（纯逻辑 + 发送流程）。"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_major_tournament.core.bracket import (  # noqa: E402
    pending_matches,
    start_tournament,
)
from astrbot_plugin_major_tournament.core.models import (  # noqa: E402
    STATUS_RUNNING,
    Tournament,
)
from astrbot_plugin_major_tournament.core.qq_official_buttons import (  # noqa: E402
    add_passive_reply_context,
    build_command_button,
    build_panel_payload,
    extract_message_reference_id,
    is_qq_official_platform,
)


def make_tournament(count=8, size=8, name="测试杯") -> Tournament:
    t = Tournament(group_id="g1", name=name, size=size)
    for i in range(count):
        t.add_player(f"u{i + 1}", f"P{i + 1}")
    return t


def all_buttons(payload):
    return [
        button
        for row in payload["keyboard"]["content"]["rows"]
        for button in row["buttons"]
    ]


def datas(payload):
    return [button["action"]["data"] for button in all_buttons(payload)]


def test_command_button_structure():
    button = build_command_button("bid", "标签", "major 报名", visited_label="已报名")
    assert button["id"] == "bid"
    assert button["render_data"]["label"] == "标签"
    assert button["render_data"]["visited_label"] == "已报名"
    assert button["action"]["type"] == 2
    assert button["action"]["data"] == "major 报名"
    assert button["action"]["reply"] is True
    assert button["action"]["enter"] is False
    assert button["action"]["permission"] == {"type": 2}


def test_registration_panel_has_signup_and_admin_buttons():
    t = make_tournament()
    payload = build_panel_payload(t, is_admin=True)
    data = datas(payload)

    assert payload["msg_type"] == 2
    assert "major 报名" in data
    assert "major 退赛" in data
    assert "major 名单" in data
    assert "major 开赛" in data
    assert "major 重置" in data
    assert len(payload["keyboard"]["content"]["rows"]) <= 5


def test_non_admin_registration_panel_has_no_admin_buttons():
    t = make_tournament()
    data = datas(build_panel_payload(t, is_admin=False))
    assert "major 报名" in data
    assert "major 开赛" not in data
    assert "major 重置" not in data


def test_running_panel_exposes_winner_buttons_for_admin():
    t = make_tournament()
    start_tournament(t, size=8, seed_mode="register")
    first = pending_matches(t)[0]
    payload = build_panel_payload(t, is_admin=True)
    data = datas(payload)

    assert t.status == STATUS_RUNNING
    assert f"major 胜 {first.match_id} 1" in data
    assert f"major 胜 {first.match_id} 2" in data
    assert len(payload["keyboard"]["content"]["rows"]) <= 5


def test_non_admin_running_panel_has_no_winner_buttons():
    t = make_tournament()
    start_tournament(t, size=8, seed_mode="register")
    data = datas(build_panel_payload(t, is_admin=False))
    assert not any(item.startswith("major 胜") for item in data)
    assert "major 名单" in data
    assert "major 对阵" in data


def test_passive_reply_context_and_reference_id():
    payload = add_passive_reply_context({"msg_type": 2}, msg_id="mid-1", msg_seq=7)
    assert payload["msg_id"] == "mid-1"
    assert payload["msg_seq"] == 7

    payload2 = add_passive_reply_context({"msg_type": 2}, event_id="evt-1")
    assert payload2["event_id"] == "evt-1"
    assert 1 <= payload2["msg_seq"] <= 10000

    raw = SimpleNamespace(id="raw-mid")
    obj = SimpleNamespace(message_id="obj-mid")
    assert extract_message_reference_id(raw, obj) == "raw-mid"
    assert extract_message_reference_id(None, obj) == "obj-mid"


def test_is_qq_official_platform():
    assert is_qq_official_platform("qq_official")
    assert is_qq_official_platform("QQ_Official_Webhook")
    assert not is_qq_official_platform("aiocqhttp")


# ─────────────── 发送流程（依赖 AstrBot 运行时） ───────────────


class FakeBotApi:
    def __init__(self):
        self.calls = []

    async def post_group_message(self, **payload):
        self.calls.append(("group", payload))


class FakeRawMessage:
    def __init__(self):
        self.id = "raw-mid"
        self.msg_seq = 5
        self.group_openid = "group-openid"


class FakeQQEvent:
    def __init__(self, text="", admin=False):
        self._text = text
        self._admin = admin
        self.stopped = False
        self.bot = SimpleNamespace(api=FakeBotApi())
        self.message_obj = SimpleNamespace(
            raw_message=FakeRawMessage(), message_id="obj-mid"
        )

    def get_platform_name(self):
        return "qq_official"

    def get_message_str(self):
        return self._text

    def get_group_id(self):
        return "group-openid"

    def get_platform_id(self):
        return "qqplat"

    def get_sender_id(self):
        return "admin" if self._admin else "u1"

    def get_sender_name(self):
        return "管理员" if self._admin else "选手1"

    def is_admin(self):
        return self._admin

    def should_call_llm(self, *_):
        return None

    def get_messages(self):
        return []

    def plain_result(self, text):
        return ("text", text)

    def stop_event(self):
        self.stopped = True


def _make_plugin(tmp_path):
    from astrbot_plugin_major_tournament.core.database import TournamentDatabase
    from astrbot_plugin_major_tournament.main import MajorTournament

    plugin = MajorTournament(None, {})
    plugin.store = TournamentDatabase(tmp_path / "test.db")
    return plugin


def test_panel_command_sends_keyboard(tmp_path):
    plugin = _make_plugin(tmp_path)
    event = FakeQQEvent(text="major面板")

    async def scenario():
        return [item async for item in plugin.major_panel(event)]

    results = asyncio.run(scenario())
    assert results == []
    assert len(event.bot.api.calls) == 1
    scene, payload = event.bot.api.calls[0]
    assert scene == "group"
    assert payload["group_openid"] == "group-openid"
    assert payload["msg_type"] == 2
    assert payload["msg_id"] == "raw-mid"
    assert payload["keyboard"]["content"]["rows"]
    assert event.stopped is True


def test_bare_major_sends_panel_on_qq_official(tmp_path):
    plugin = _make_plugin(tmp_path)
    event = FakeQQEvent(text="major")

    async def scenario():
        return [item async for item in plugin.major(event)]

    results = asyncio.run(scenario())
    assert results == []
    assert len(event.bot.api.calls) == 1
    assert event.bot.api.calls[0][1]["msg_type"] == 2


def test_button_press_data_runs_real_action(tmp_path):
    """按钮 data 就是命令文本，点击后走原有命令逻辑。"""
    plugin = _make_plugin(tmp_path)
    event = FakeQQEvent(text="major 报名")

    async def scenario():
        return [item async for item in plugin.major(event)]

    results = asyncio.run(scenario())
    assert any("报名成功" in item[1] for item in results)
    # 报名后面板自动刷新
    assert event.bot.api.calls
    assert event.bot.api.calls[-1][1]["msg_type"] == 2

    tournament = plugin.store.load("group-openid", "qqplat")
    assert tournament is not None
    assert tournament.player_count == 1
