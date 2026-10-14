"""头像 URL / data URI / 渲染嵌入测试。"""

import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_major_tournament.core.avatars import (  # noqa: E402
    build_avatar_url,
    to_data_uri,
)
from astrbot_plugin_major_tournament.core.bracket import (  # noqa: E402
    start_tournament,
)
from astrbot_plugin_major_tournament.core.models import Tournament  # noqa: E402
from astrbot_plugin_major_tournament.core.renderer import (  # noqa: E402
    BracketRenderer,
)


def test_build_avatar_url_qq_official_needs_appid():
    url = build_avatar_url("ABC123OPENID", "qq_official", "1029384756")
    assert url == "https://thirdqq.qlogo.cn/qqapp/1029384756/ABC123OPENID/640"
    # 没有 appid 时无法拼官方头像
    assert build_avatar_url("ABC123OPENID", "qq_official", "") is None


def test_build_avatar_url_onebot_qq_number():
    assert (
        build_avatar_url("10001", "aiocqhttp")
        == "https://q1.qlogo.cn/g?b=qq&nk=10001&s=640"
    )
    # 非 QQ 号 / 非 QQ 平台无法拼地址
    assert build_avatar_url("abc-openid", "aiocqhttp") is None
    assert build_avatar_url("10001", "telegram") is None


def test_to_data_uri_resizes_image():
    buffer = BytesIO()
    Image.new("RGB", (256, 256), (200, 30, 30)).save(buffer, format="PNG")
    data_uri = to_data_uri(buffer.getvalue(), size=48)
    assert data_uri is not None
    assert data_uri.startswith("data:image/png;base64,")

    import base64

    raw = base64.b64decode(data_uri.split(",", 1)[1])
    with Image.open(BytesIO(raw)) as image:
        assert max(image.size) <= 48


def test_to_data_uri_rejects_non_image():
    assert to_data_uri(b"not an image") is None
    assert to_data_uri(b"") is None


def test_renderer_embeds_avatar_when_provided():
    tournament = Tournament(group_id="g", name="杯", size=4)
    for i in range(4):
        tournament.add_player(f"u{i}", f"P{i}")
    start_tournament(tournament, 4, seed_mode="register")

    renderer = BracketRenderer(Path(__file__).resolve().parents[1] / "templates")
    fake_uri = "data:image/png;base64,AAAA"
    html = renderer.render_html(tournament, avatar_map={"u0": fake_uri})

    assert f'src="{fake_uri}"' in html
    assert 'class="logo"' in html


def test_bracket_layout_aligns_rounds():
    """每一列卡片应精确落在上一列两张卡的中点（对阵树对齐）。"""
    tournament = Tournament(group_id="g", name="杯", size=8)
    for i in range(8):
        tournament.add_player(f"u{i}", f"P{i}")
    start_tournament(tournament, 8, seed_mode="register")

    renderer = BracketRenderer(Path(__file__).resolve().parents[1] / "templates")
    layout = renderer.build_bracket_layout(tournament)
    columns = layout["columns"]

    assert [col["name"] for col in columns] == ["8强", "4强", "决赛", "冠军"]
    assert [len(col["cards"]) for col in columns] == [8, 4, 2, 1]
    assert layout["connectors"]

    for prev_col, next_col in zip(columns, columns[1:], strict=False):
        prev_tops = [card["top"] for card in prev_col["cards"]]
        next_tops = [card["top"] for card in next_col["cards"]]
        for index, top in enumerate(next_tops):
            expect = (prev_tops[2 * index] + prev_tops[2 * index + 1]) / 2
            assert abs(top - expect) < 0.01


def test_bracket_layout_for_32_players():
    tournament = Tournament(group_id="g", name="杯", size=32)
    for i in range(32):
        tournament.add_player(f"u{i}", f"P{i}")
    start_tournament(tournament, 32, seed_mode="register")

    renderer = BracketRenderer(Path(__file__).resolve().parents[1] / "templates")
    layout = renderer.build_bracket_layout(tournament)
    assert [len(col["cards"]) for col in layout["columns"]] == [
        32,
        16,
        8,
        4,
        2,
        1,
    ]
    assert layout["width"] > 0 and layout["height"] > 0


def test_resolve_appid_from_platform_instance():
    from astrbot_plugin_major_tournament.main import MajorTournament

    plugin = MajorTournament(None, {})
    platform = SimpleNamespace(
        meta=lambda: SimpleNamespace(id="default_123", name="qq_official"),
        config={"appid": "123456"},
    )
    plugin.context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=[platform])
    )
    event = SimpleNamespace(get_platform_id=lambda: "default_123")
    assert plugin._resolve_appid(event) == "123456"


def test_viewport_fits_content_to_avoid_blank():
    """视口应贴合内容，避免 T2I 默认 1280x720 留下大片空白。"""
    tournament = Tournament(group_id="g", name="杯", size=8)
    for i in range(8):
        tournament.add_player(f"u{i}", f"P{i}")
    start_tournament(tournament, 8, seed_mode="register")

    renderer = BracketRenderer(Path(__file__).resolve().parents[1] / "templates")
    context = renderer.build_context(tournament)
    layout = context["b"]

    assert context["viewport_w"] == layout["width"] + 32
    assert context["viewport_h"] == layout["height"] + 100

    html = renderer.render_html(tournament)
    assert f"width={context['viewport_w']}" in html
    assert f"height={context['viewport_h']}" in html


def test_render_profile_uses_neon_card_fields():
    """名片模板应渲染出科技感元素与关键字段。"""
    import tempfile

    from astrbot_plugin_major_tournament.core.database import (
        TournamentDatabase,
    )

    with tempfile.TemporaryDirectory() as tmp:
        db = TournamentDatabase(f"{tmp}/m.db")
        t = Tournament(group_id="g", platform_id="p", name="霓虹杯", size=4)
        t.add_player("u1", "未来")
        for i in range(3):
            t.add_player(f"x{i}", f"路人{i}")
        db.save(t)
        loaded = db.load("g", "p")
        start_tournament(loaded, 4, seed_mode="register")
        from astrbot_plugin_major_tournament.core.bracket import (
            pending_matches,
            set_winner,
        )

        guard = 0
        while loaded.status != "finished" and guard < 10:
            guard += 1
            matches = pending_matches(loaded)
            if not matches:
                break
            set_winner(loaded, matches[0].match_id, "1")
        db.save(loaded)
        db.delete("g", "p")

        summary = db.player_summary("g", "p", loaded.champion)
        renderer = BracketRenderer(Path(__file__).resolve().parents[1] / "templates")
        html = renderer.render_profile(summary, {}, updated_at="2026-11-05 12:00")

    # 科技感元素
    assert "orbit" in html
    assert "corner tl" in html
    assert "accentline" in html
    assert "MAJOR / 赛制" in html
    assert "PLAYER PROFILE" in html
    # 关键字段
    assert summary["name"] in html
    assert "参赛场次" in html and "冠军" in html and "亚军" in html
    assert "DIGITAL PLAYER ID" in html
