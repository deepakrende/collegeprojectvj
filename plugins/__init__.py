# Don't Remove Credit @VJ_Bots
# Subscribe YouTube Channel For Amazing Bot @Tech_VJ
# Ask Doubt on telegram @KingVJ01

from aiohttp import web
from .route import routes

async def web_server():
    web_app = web.Application(client_max_size=30000000)

    # ✅ ADD THIS PART
    async def root(request):
        return web.Response(text="Bot is running")

    web_app.router.add_get("/", root)

    # existing routes
    web_app.add_routes(routes)

    return web_app
