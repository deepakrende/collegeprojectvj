# Don't Remove Credit @VJ_Bots
# Subscribe YouTube Channel For Amazing Bot @Tech_VJ
# Ask Doubt on telegram @KingVJ01

import re
import jinja2
import urllib.parse
import logging
import aiohttp
from urllib.parse import quote_plus

from info import *
from TechVJ.bot import TechVJBot
from TechVJ.util.human_readable import humanbytes
from TechVJ.util.file_properties import get_file_ids, get_hash, get_name
from TechVJ.server.exceptions import InvalidHash, FIleNotFound
from database.ia_filterdb import col, sec_col, MULTIPLE_DATABASE
from TechVJ.util.link_utils import make_link
from database.connections_mdb import increment_video_view, get_video_stats

QUALITIES = ["360p", "480p", "720p", "1080p", "1440p", "2160p"]
QUALITY_RE = re.compile(r"(?<!\d)(360p|480p|720p|1080p|1440p|2160p)(?!\d)", re.I)


def _normalize_quality_name(name: str) -> str:
    name = str(name or "").lower()
    name = re.sub(r"\.[a-z0-9]{2,5}$", "", name)
    name = re.sub(r"@vj_bots\s*", " ", name, flags=re.I)
    name = QUALITY_RE.sub(" ", name)
    name = re.sub(r"[._+\-\[\](){}]+", " ", name)
    return " ".join(name.split())


def _quality_from_name(name: str) -> str:
    match = QUALITY_RE.search(str(name or ""))
    return match.group(1).lower() if match else ""


def _quality_candidates(current_name: str):
    base = _normalize_quality_name(current_name)
    if not base:
        return {}

    # Search using a few stable title words, then compare normalized names exactly.
    words = [w for w in base.split() if len(w) > 1]
    search_term = " ".join(words[:6])
    if not search_term:
        return {}

    try:
        import asyncio
        # This helper is called inside an async function; the actual DB query is done below.
    except Exception:
        pass
    return base, search_term


async def _find_quality_files(current_name: str):
    info = _quality_candidates(current_name)
    if not info:
        return {}
    base, search_term = info

    # Reuse the same search behavior as the bot. A small candidate set keeps the watch page fast.
    from database.ia_filterdb import get_search_results
    files, _, _ = await get_search_results(0, search_term, max_results=100, offset=0, filter=True)

    variants = {}
    for item in files:
        name = item.get("file_name", "")
        quality = _quality_from_name(name)
        if not quality:
            continue
        if _normalize_quality_name(name) != base:
            continue
        variants.setdefault(quality, item)
    return variants


async def _build_quality_options(current_id, current_name, current_hash, expires, selected_quality=""):
    variants = await _find_quality_files(current_name)
    current_quality = _quality_from_name(current_name)
    if current_quality:
        # Ensure the current file is represented even if it isn't in the index.
        variants.setdefault(current_quality, None)

    if len(variants) < 2:
        return []

    options = []
    for quality in QUALITIES:
        if quality not in variants:
            continue
        item = variants[quality]
        if item is None:
            item_id = current_id
            item_hash = current_hash
            item_name = current_name
        else:
            # The DB stores cached Telegram file IDs, so convert the selected quality
            # to a temporary LOG_CHANNEL message only when the user selects it.
            item_id = current_id
            item_hash = current_hash
            item_name = item.get("file_name", current_name)

        options.append({
            "quality": quality,
            "label": quality.upper(),
            "active": quality == (selected_quality or current_quality),
            "item_file_id": item.get("file_id") if item else None,
            "item_name": item_name,
            "url": "",  # Filled by the route/template using quality query.
        })
    return options


async def resolve_quality_variant(current_id, current_name, quality):
    quality = str(quality or "").lower()
    if quality not in QUALITIES:
        return current_id, current_name

    variants = await _find_quality_files(current_name)
    item = variants.get(quality)
    if not item:
        return current_id, current_name

    # Send the cached file to the log channel so the existing media streamer
    # can continue to use its normal message-id + hash mechanism.
    log_msg = await TechVJBot.send_cached_media(chat_id=LOG_CHANNEL, file_id=item["file_id"])
    return log_msg.id, get_name(log_msg)


async def render_page(id, secure_hash, expires=None, signature=None, quality=None):
    file = await TechVJBot.get_messages(int(LOG_CHANNEL), int(id))
    if file.empty:
        raise FIleNotFound

    file_data = await get_file_ids(TechVJBot, int(LOG_CHANNEL), int(id))
    if file_data.unique_id[:6] != secure_hash:
        logging.debug(f"link hash: {secure_hash} - {file_data.unique_id[:6]}")
        logging.debug(f"Invalid hash for message with - ID {id}")
        raise InvalidHash

    current_name = file_data.file_name or ""
    # Count one view when the watch page is successfully opened.
    stats = await get_video_stats(id)
    await increment_video_view(id)
    stats["views"] = int(stats.get("views", 0)) + 1
    selected_quality = str(quality or "").lower()

    if selected_quality:
        new_id, new_name = await resolve_quality_variant(id, current_name, selected_quality)
        if new_id != id:
            id = new_id
            current_name = new_name
            file_data = await get_file_ids(TechVJBot, int(LOG_CHANNEL), int(id))
            secure_hash = file_data.unique_id[:6]

    src = make_link(
        "watch" if False else "",
        id,
        urllib.parse.quote_plus(file_data.file_name),
        secure_hash,
        int(expires),
        selected_quality,
    )
    # make_link with an empty path produces the media URL. The watch URL remains
    # the current page URL and is handled by the route.

    tag = file_data.mime_type.split("/")[0].strip()
    file_size = humanbytes(file_data.file_size)
    quality_options = []
    variants = await _find_quality_files(file_data.file_name)
    current_quality = _quality_from_name(file_data.file_name)
    if len(variants) >= 2:
        for q in QUALITIES:
            if q in variants or q == current_quality:
                quality_options.append({
                    "quality": q,
                    "label": q.upper(),
                    "active": q == current_quality,
                    "url": "?quality=" + q,
                })

    if tag in ["video", "audio"]:
        template_file = "TechVJ/template/req.html"
    else:
        template_file = "TechVJ/template/dl.html"
        async with aiohttp.ClientSession() as s:
            async with s.get(src) as u:
                file_size = humanbytes(int(u.headers.get("Content-Length")))

    with open(template_file, encoding="utf-8") as f:
        template = jinja2.Template(f.read())

    file_name = file_data.file_name.replace("_", " ")
    quality_query = f"&quality={urllib.parse.quote_plus(q)}" if selected_quality else ""

    # Build signed watch URLs for each available quality.
    for option in quality_options:
        option["url"] = make_link(
            "watch",
            id,
            urllib.parse.quote_plus(file_data.file_name),
            secure_hash,
            int(expires),
            option["quality"],
        )

    # The browser should request the current media URL with the current signed token.
    # Media is now delivered through a short Railway redirect to the private
    # Storage Bucket. The browser receives the actual video bytes directly
    # from the bucket, so Railway does not pay per-view egress.
    media_url = make_link(
        "media",
        id,
        urllib.parse.quote_plus(file_data.file_name),
        secure_hash,
        int(expires),
        selected_quality,
    )

    return template.render(
        file_name=file_name,
        file_url=media_url,
        file_mime=file_data.mime_type or "application/octet-stream",
        file_size=file_size,
        file_unique_id=file_data.unique_id,
        quality_options=quality_options,
        link_expires=int(expires),
        selected_quality=current_quality,
        file_id=id,
        total_views=int(stats.get("views", 0)),
        total_downloads=int(stats.get("downloads", 0)),
        quality_query=quality_query,
    )
