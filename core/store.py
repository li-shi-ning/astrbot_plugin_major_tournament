"""赛事持久化：每个群一份 JSON 文件。"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from .models import Tournament


def _safe_name(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+", "_", str(value or ""))
    return text.strip("_") or "unknown"


class TournamentStore:
    """简单的 JSON 文件存储。

    key = platform_id + group_id，保证多平台/多群互不干扰。
    """

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, group_id: str, platform_id: str = "") -> Path:
        return self.data_dir / f"{_safe_name(platform_id)}__{_safe_name(group_id)}.json"

    def load(self, group_id: str, platform_id: str = "") -> Tournament | None:
        path = self._path(group_id, platform_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return Tournament.from_dict(data)

    def save(self, tournament: Tournament) -> None:
        tournament.touch()
        path = self._path(tournament.group_id, tournament.platform_id)
        payload = json.dumps(tournament.to_dict(), ensure_ascii=False, indent=2)
        # 原子写，避免渲染/断电时写坏文件
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.data_dir, delete=False
        ) as handle:
            handle.write(payload)
            temp_path = Path(handle.name)
        temp_path.replace(path)

    def delete(self, group_id: str, platform_id: str = "") -> bool:
        path = self._path(group_id, platform_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_groups(self) -> list[str]:
        return [p.stem for p in sorted(self.data_dir.glob("*.json"))]
