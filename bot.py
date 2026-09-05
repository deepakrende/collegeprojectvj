# Don't Remove Credit @VJ_Bots
# Subscribe YouTube Channel For Amazing Bot @Tech_VJ
# Ask Doubt on telegram @KingVJ01

import os
PORT = int(os.environ.get("PORT", 8080))

import sys, glob, importlib, logging, logging.config, pytz, asyncio
from pathlib import Path

# Logging
logging.config.fileConfig('logging.conf')
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.ERROR)
logging.getLogger("cinemagoer").setLevel(logging.ERROR)

from pyrogram import idle
from database.users_chats_db import db
from info import *
from utils import temp
from Script import script
from datetime import date, datetime
from aiohttp import ClientSession, web
from plugins import web_server
from plugins.clone import restart_bots

from TechVJ.bot import TechVJBot
from TechVJ.util.keepalive import ping_server
from TechVJ.bot.clients import initialize_clients

ppath = "plugins/*.py"
files = sorted(glob.glob(ppath))

TechVJBot.start()
print("✅ Bot Client Started")

# ✅ Start user client(s) if any SESSION_STRING(s) are set.
# Supports multiple sessions (SESSION_STRING1, SESSION_STRING2, ...) so
# indexing can spread across several accounts / access more channels.
UserClients = []
if SESSION_STRINGS:
    from pyrogram import Client as PyroClient

    for idx, s_string in enumerate(SESSION_STRINGS, start=1):
        try:
            client = PyroClient(
                name=f"UserSession{idx}",
                api_id=API_ID,
                api_hash=API_HASH,
                session_string=s_string,
                # 0 = never silently sleep-and-retry on a FloodWait.
                # Any FloodWait (even a short one) raises immediately
                # instead of blocking this account for that many
                # seconds, so the indexing rotation logic can switch
                # to the next account right away rather than sitting
                # idle on the flood-limited one.
                sleep_threshold=0
            )
            client.start()
            UserClients.append(client)
            print(f"✅ User Client {idx} Started Successfully")
        except Exception as e:
            print(f"❌ Failed To Start User Client {idx}: {e}")

if UserClients:
    temp.USER_CLIENT = UserClients[0]
    temp.USER_CLIENTS = UserClients
    print(f"✅ {len(UserClients)} User Client(s) Ready For Indexing")
else:
    temp.USER_CLIENT = None
    temp.USER_CLIENTS = []
    print("ℹ️ No SESSION_STRING(s) set — using bot client for indexing")

loop = asyncio.get_event_loop()


def register_plugin_handlers(plugin_module):
    """Attach class-decorated Pyrogram handlers to the running bot client.

    ``@Client.on_message(...)`` used in this project stores handler metadata
    on the decorated function. The built-in Pyrogram plugin loader normally
    registers that metadata, but these modules are loaded manually below.
    Without this step the bot connects successfully but has no update handlers
    and therefore cannot respond to /start or any other command.
    """
    handler_count = 0
    for value in vars(plugin_module).values():
        # ``getattr(value, "handlers", [])`` can return something other than
        # a list of (handler, group) tuples if a plugin's module-level name
        # happens to collide with an unrelated object that also has a
        # ``.handlers`` attribute (e.g. a bare ``import pyrogram`` binds the
        # ``pyrogram`` package, which itself has a ``handlers`` submodule;
        # the same can happen with ``logging`` once ``logging.handlers`` has
        # been imported anywhere in the process). Only treat it as handler
        # metadata when it's actually a list/tuple of 2-tuples, and skip
        # anything else instead of crashing the whole bot on startup.
        handlers = getattr(value, "handlers", None)
        if not isinstance(handlers, (list, tuple)):
            continue
        for entry in handlers:
            try:
                handler, group = entry
            except (TypeError, ValueError):
                continue
            TechVJBot.add_handler(handler, group)
            handler_count += 1
    return handler_count


async def send_restart_notice(text):
    """Send the startup notice, including on fresh ephemeral bot sessions.

    Pyrogram uses MTProto and needs a cached access hash for a numeric channel
    ID. Fresh bot sessions may not have one and receive CHANNEL_INVALID even
    when the bot is an administrator. Telegram's Bot API accepts the numeric
    chat ID directly, so use it as the fallback.
    """
    try:
        await TechVJBot.send_message(chat_id=LOG_CHANNEL, text=text)
        logging.info("Sent restart notice to log channel %s via Pyrogram", LOG_CHANNEL)
        return
    except Exception:
        logging.warning(
            "Pyrogram could not resolve log channel %s; trying the Telegram Bot API.",
            LOG_CHANNEL,
            exc_info=True,
        )

    try:
        api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": str(LOG_CHANNEL),
            "text": text,
            "parse_mode": "HTML",
        }
        async with ClientSession() as session:
            async with session.post(api_url, json=payload) as response:
                result = await response.json(content_type=None)

        if not result.get("ok"):
            raise RuntimeError(result.get("description", "Unknown Bot API error"))

        logging.info("Sent restart notice to log channel %s via Bot API", LOG_CHANNEL)
    except Exception:
        logging.exception(
            "Could not send restart notice to log channel %s via Pyrogram or "
            "the Telegram Bot API.",
            LOG_CHANNEL,
        )


async def restart_clone_bots_in_background():
    """Restore optional clone bots without delaying the primary bot."""
    try:
        await restart_bots()
        logging.info("Finished restarting clone bots")
    except Exception:
        logging.exception("Clone-bot restart failed; primary bot remains available")


async def start():
    print('\n🚀 Initializing Your Bot')

    print("🔥 Starting Web Server FIRST...")

    app = web.AppRunner(await web_server())
    await app.setup()
    
    bind_address = "0.0.0.0"
    
    print("🌐 PORT FROM RAILWAY:", PORT)
    
    site = web.TCPSite(app, bind_address, PORT)
    await site.start()
    
    print(f"✅ Web Server Running on http://0.0.0.0:{PORT}")

    # NOW start bot
    bot_info = await TechVJBot.get_me()
    await initialize_clients()

# Load plugins
    for name in files:
        with open(name) as a:
            patt = Path(a.name)
            plugin_name = patt.stem.replace(".py", "")
            plugins_dir = Path(f"plugins/{plugin_name}.py")
            import_path = "plugins.{}".format(plugin_name)
            spec = importlib.util.spec_from_file_location(import_path, plugins_dir)
            load = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(load)
            sys.modules["plugins." + plugin_name] = load
            handler_count = register_plugin_handlers(load)
            logging.info(
                "Imported plugin %s and registered %s handler(s)",
                plugin_name,
                handler_count,
            )

# Keep alive (optional)
    if ON_HEROKU:
        asyncio.create_task(ping_server())

# DB
    b_users, b_chats = await db.get_banned()
    temp.BANNED_USERS = b_users
    temp.BANNED_CHATS = b_chats
    
    me = await TechVJBot.get_me()
    temp.BOT = TechVJBot
    temp.ME = me.id
    temp.U_NAME = me.username
    temp.B_NAME = me.first_name
    logging.info("Connected to Telegram as @%s (ID: %s)", me.username, me.id)
    
    logging.info(script.LOGO)
    
    # Restart logs
    tz = pytz.timezone('Asia/Kolkata')
    today = date.today()
    now = datetime.now(tz)
    time = now.strftime("%H:%M:%S %p")

    await send_restart_notice(script.RESTART_TXT.format(today, time))
    
    # Do not send/delete startup test messages in file or force-subscribe
    # channels. Fresh Pyrogram sessions can spend time resolving these numeric
    # IDs, delaying the main bot from reaching idle().
    logging.info(
        "Startup complete; configured %s file channel(s)%s.",
        len(CHANNELS),
        " and a force-subscribe channel" if AUTH_CHANNEL else "",
    )
    
    # Clone bots
    if CLONE_MODE:
        logging.info("Starting clone-bot restoration in the background")
        asyncio.create_task(restart_clone_bots_in_background())

    logging.info("Bot is ready and waiting for Telegram updates")
    await idle()
    logging.info("Bot idle() exited")


if __name__ == '__main__':
    try:
        loop.run_until_complete(start())
    except KeyboardInterrupt:
        logging.info('Service Stopped Bye 👋')
    except Exception:
        logging.exception('Fatal error during bot startup')
        raise
