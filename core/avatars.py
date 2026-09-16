"""选手头像获取：拼 URL → 下载 → 缩放 → base64 data URI。

对阵图由远端 T2I 渲染，所以不能直接引用本机地址；这里把头像下载后
转成 data URI 内嵌进 HTML，T2I 无需再访问外网即可出图。
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Iterable
from io import BytesIO
from urllib.parse import quote

import aiohttp
from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

#: 头像缩略图边长（px）
AVATAR_THUMB_SIZE = 72

#: 并发下载数
_MAX_CONCURRENCY = 6

QQ_OFFICIAL_PLATFORMS = {"qq_official", "qq_official_webhook"}

#: OneBot 系平台（user_id 才是 QQ 号）
ONEBOT_PLATFORMS = {
    "aiocqhttp",
    "onebot",
    "onebot_v11",
    "onebot11",
    "napcat",
    "napcatqq",
    "llonebot",
    "lagrange",
    "snowluma",
}


def build_avatar_url(user_id: str, platform_name: str, appid: str = "") -> str | None:
    """根据平台与用户 ID 拼出头像地址。

    - QQ 官方机器人：``https://thirdqq.qlogo.cn/qqapp/{appid}/{openid}/640``
      （openid 不是 QQ 号，必须配合 appid）
    - OneBot / 其它拿得到 QQ 号的平台：``q1.qlogo.cn`` 老接口
    """
    uid = str(user_id or "").strip()
    if not uid:
        return None

    name = str(platform_name or "").strip().lower()
    if name in QQ_OFFICIAL_PLATFORMS:
        if not appid:
            return None
        return (
            "https://thirdqq.qlogo.cn/qqapp/"
            f"{quote(str(appid), safe='')}/{quote(uid, safe='')}/640"
        )

    is_onebot = name in ONEBOT_PLATFORMS or "onebot" in name
    if is_onebot and uid.isdigit() and 5 <= len(uid) <= 12:
        return f"https://q1.qlogo.cn/g?b=qq&nk={uid}&s=640"
    return None


def to_data_uri(payload: bytes, size: int = AVATAR_THUMB_SIZE) -> str | None:
    """把图片字节转成 data URI（同时缩放到统一尺寸）。"""
    if not payload:
        return None
    try:
        with Image.open(BytesIO(payload)) as image:
            image = image.convert("RGBA")
            # 透明底合成到深色背景，避免渲染出黑边
            background = Image.new("RGB", image.size, (18, 26, 44))
            background.paste(image, mask=image.split()[-1])
            background.thumbnail((size, size), Image.Resampling.LANCZOS)
            buffer = BytesIO()
            background.save(buffer, format="PNG", optimize=True)
    except (OSError, UnidentifiedImageError, ValueError):
        return None
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


class AvatarFetcher:
    """带内存缓存与并发限制的头像下载器。"""

    def __init__(self, timeout_seconds: float = 12.0):
        self._timeout = timeout_seconds
        self._cache: dict[str, str] = {}
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    trust_env=True,
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                )
            return self._session

    async def close(self) -> None:
        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                await self._session.close()
            self._session = None

    async def fetch(self, url: str) -> str | None:
        if not url:
            return None
        cached = self._cache.get(url)
        if cached:
            return cached
        try:
            session = await self._get_session()
            async with self._semaphore:
                async with session.get(url) as response:
                    if response.status != 200:
                        return None
                    payload = await response.read()
        except Exception as exc:  # noqa: BLE001 - 单个头像失败不影响整体
            logger.debug(f"[Major] 下载头像失败 {url}: {exc}")
            return None

        data_uri = to_data_uri(payload)
        if data_uri:
            self._cache[url] = data_uri
        return data_uri

    async def fetch_many(self, urls: Iterable[str | None]) -> list[str | None]:
        targets = list(urls)
        if not targets:
            return []
        return list(await asyncio.gather(*(self.fetch(url or "") for url in targets)))
