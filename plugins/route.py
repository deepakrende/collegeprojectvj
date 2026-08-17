# Don't Remove Credit @VJ_Bots
# Subscribe YouTube Channel For Amazing Bot @Tech_VJ
# Ask Doubt on telegram @KingVJ01

import re, math, logging, secrets, mimetypes, time, json
from info import *
from aiohttp import web
from aiohttp.http_exceptions import BadStatusLine
from TechVJ.bot import multi_clients, work_loads, TechVJBot
from TechVJ.server.exceptions import FIleNotFound, InvalidHash
from TechVJ import StartTime, __version__
from TechVJ.util.custom_dl import ByteStreamer
from TechVJ.util.time_format import get_readable_time
from TechVJ.util.render_template import render_page
from TechVJ.util.link_utils import validate_link
from TechVJ.util.bucket_storage import bucket_enabled, get_presigned_url
from database.connections_mdb import increment_video_download

routes = web.RouteTableDef()


def _link_is_valid(request, message_id, secure_hash, quality=""):
    expires = request.rel_url.query.get("exp")
    signature = request.rel_url.query.get("sig")
    return validate_link(message_id, secure_hash, expires, signature, quality)


def _expired_response():
    return web.Response(
        status=410,
        text="This link has expired. Please generate a new link.",
        content_type="text/plain",
    )


@routes.get("/", allow_head=True)
async def root_route_handler(request):
    return web.json_response("BenFilterBot")


@routes.get(r"/watch/{path:\S+}", allow_head=True)
async def stream_handler(request: web.Request):
    try:
        path = request.match_info["path"]
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)$", path)
        if match:
            secure_hash = match.group(1)
            id = int(match.group(2))
        else:
            id = int(re.search(r"(\d+)(?:/\S+)?", path).group(1))
            secure_hash = request.rel_url.query.get("hash")

        quality = request.rel_url.query.get("quality", "").lower()
        if not _link_is_valid(request, id, secure_hash, quality):
            return _expired_response()

        return web.Response(
            text=await render_page(
                id,
                secure_hash,
                request.rel_url.query.get("exp"),
                request.rel_url.query.get("sig"),
                quality,
            ),
            content_type="text/html",
        )
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except (AttributeError, BadStatusLine, ConnectionResetError):
        pass
    except Exception as e:
        logging.critical(e.with_traceback(None))
        raise web.HTTPInternalServerError(text=str(e))


@routes.get(r"/media/{path:\S+}", allow_head=True)
async def bucket_media_handler(request: web.Request):
    """Validate the signed link, then redirect the browser directly to the Railway Bucket.

    The Railway service never sends the video bytes to the viewer. The first request
    migrates the Telegram file into the bucket (one-time service egress); subsequent
    requests only return a short-lived presigned bucket URL.
    """
    try:
        path = request.match_info["path"].lstrip("/")
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)(?:/.*)?$", path)
        if match:
            secure_hash = match.group(1)
            file_id = int(match.group(2))
        else:
            match_id = re.search(r"(\d+)(?:/\S+)?", path)
            if not match_id:
                raise web.HTTPBadRequest(text="Invalid media path")
            file_id = int(match_id.group(1))
            secure_hash = request.rel_url.query.get("hash")

        quality = request.rel_url.query.get("quality", "").lower()
        expires = request.rel_url.query.get("exp")
        signature = request.rel_url.query.get("sig")
        if not validate_link(file_id, secure_hash, expires, signature, quality):
            return _expired_response()

        if not bucket_enabled():
            # Fail closed: never fall back to Telegram -> Railway -> user streaming,
            # otherwise the Railway egress bill can grow without bound.
            raise web.HTTPServiceUnavailable(
                text="Video delivery storage is temporarily unavailable. Please try again later."
            )

        started = time.monotonic()
        logging.info("Preparing bucket media: file_id=%s", file_id)
        target = await get_presigned_url(file_id, int(expires))
        logging.info(
            "Bucket media ready: file_id=%s elapsed=%.2fs",
            file_id,
            time.monotonic() - started,
        )
        raise web.HTTPFound(location=target)
    except web.HTTPException:
        raise
    except InvalidHash as e:
        raise web.HTTPForbidden(text=e.message)
    except FIleNotFound as e:
        raise web.HTTPNotFound(text=e.message)
    except Exception as e:
        logging.exception("Bucket media delivery failed")
        raise web.HTTPServiceUnavailable(text="Unable to prepare this video for delivery.")


@routes.get(r"/{path:\S+}", allow_head=True)
async def legacy_media_redirect_handler(request: web.Request):
    """Preserve older media links without streaming bytes through Railway."""
    try:
        path = request.match_info["path"].lstrip("/")
        match = re.search(r"^([a-zA-Z0-9_-]{6})(\d+)(?:/.*)?$", path)
        if match:
            secure_hash = match.group(1)
            file_id = int(match.group(2))
        else:
            match_id = re.search(r"(\d+)(?:/\S+)?", path)
            if not match_id:
                raise web.HTTPNotFound(text="File not found")
            file_id = int(match_id.group(1))
            secure_hash = request.rel_url.query.get("hash")

        quality = request.rel_url.query.get("quality", "").lower()
        if not validate_link(
            file_id,
            secure_hash,
            request.rel_url.query.get("exp"),
            request.rel_url.query.get("sig"),
            quality,
        ):
            return _expired_response()

        # Normalize accidental leading slashes so old links never become
        # /media//664/... or /media////664/....
        target = request.rel_url.with_path("/media/" + path.lstrip("/"))
        raise web.HTTPFound(location=str(target))
    except web.HTTPException:
        raise
    except Exception as e:
        logging.exception("Legacy media redirect failed")
        raise web.HTTPInternalServerError(text="Unable to redirect this media link.")


# In-memory active viewer registry: {video_id: {viewer_id: last_seen_monotonic}}
_ACTIVE_VIEWERS = {}
_VIEWER_TTL = 35

def _viewer_count(video_id):
    now = time.monotonic()
    viewers = _ACTIVE_VIEWERS.setdefault(int(video_id), {})
    stale = [k for k, v in viewers.items() if now - v > _VIEWER_TTL]
    for k in stale:
        viewers.pop(k, None)
    if not viewers:
        _ACTIVE_VIEWERS.pop(int(video_id), None)
        return 0
    return len(viewers)


@routes.get(r"/api/viewers/{file_id}")
async def viewer_count_handler(request: web.Request):
    try:
        file_id = int(request.match_info["file_id"])
        return web.json_response({"viewers": _viewer_count(file_id)})
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Invalid file ID")


@routes.post(r"/api/viewers/{file_id}")
async def viewer_heartbeat_handler(request: web.Request):
    try:
        file_id = int(request.match_info["file_id"])
        data = await request.json()
        viewer_id = str(data.get("viewer_id", "")).strip()
        active = bool(data.get("active", False))
        if not viewer_id or len(viewer_id) > 128:
            raise web.HTTPBadRequest(text="Invalid viewer ID")
        viewers = _ACTIVE_VIEWERS.setdefault(file_id, {})
        if active:
            viewers[viewer_id] = time.monotonic()
        else:
            viewers.pop(viewer_id, None)
        return web.json_response({"viewers": _viewer_count(file_id)})
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Invalid file ID")


@routes.post(r"/api/download/{file_id}")
async def download_count_handler(request: web.Request):
    try:
        file_id = int(request.match_info["file_id"])
        count = await increment_video_download(file_id)
        return web.json_response({"downloads": count})
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Invalid file ID")


class_cache = {}


async def media_streamer(request: web.Request, id: int, secure_hash: str):
    range_header = request.headers.get("Range", 0)

    index = min(work_loads, key=work_loads.get)
    faster_client = multi_clients[index]

    if MULTI_CLIENT:
        logging.info(f"Client {index} is now serving {request.remote}")

    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
        logging.debug(f"Using cached ByteStreamer object for client {index}")
    else:
        logging.debug(f"Creating new ByteStreamer object for client {index}")
        tg_connect = ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect
    logging.debug("before calling get_file_properties")
    file_id = await tg_connect.get_file_properties(id)
    logging.debug("after calling get_file_properties")

    if file_id.unique_id[:6] != secure_hash:
        logging.debug(f"Invalid hash for message with ID {id}")
        raise InvalidHash

    file_size = file_id.file_size

    if range_header:
        from_bytes, until_bytes = range_header.replace("bytes=", "").split("-")
        from_bytes = int(from_bytes)
        until_bytes = int(until_bytes) if until_bytes else file_size - 1
    else:
        from_bytes = request.http_range.start or 0
        until_bytes = (request.http_range.stop or file_size) - 1

    if (until_bytes > file_size) or (from_bytes < 0) or (until_bytes < from_bytes):
        return web.Response(
            status=416,
            body="416: Range not satisfiable",
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    chunk_size = 1024 * 1024
    until_bytes = min(until_bytes, file_size - 1)

    offset = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = until_bytes % chunk_size + 1

    req_length = until_bytes - from_bytes + 1
    part_count = math.ceil(until_bytes / chunk_size) - math.floor(offset / chunk_size)
    body = tg_connect.yield_file(
        file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size
    )

    mime_type = file_id.mime_type
    file_name = file_id.file_name
    disposition = "attachment"

    if mime_type:
        if not file_name:
            try:
                file_name = f"{secrets.token_hex(2)}.{mime_type.split('/')[1]}"
            except (IndexError, AttributeError):
                file_name = f"{secrets.token_hex(2)}.unknown"
    else:
        if file_name:
            mime_type = mimetypes.guess_type(file_id.file_name)
        else:
            mime_type = "application/octet-stream"
            file_name = f"{secrets.token_hex(2)}.unknown"

    return web.Response(
        status=206 if range_header else 200,
        body=body,
        headers={
            "Content-Type": f"{mime_type}",
            "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
            "Content-Length": str(req_length),
            "Content-Disposition": f'{disposition}; filename="{file_name}"',
            "Accept-Ranges": "bytes",
        },
    )
