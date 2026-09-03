# Don't Remove Credit @VJ_Bots
# Subscribe YouTube Channel For Amazing Bot @Tech_VJ
# Ask Doubt on telegram @KingVJ01

import logging, re, asyncio
from utils import temp
from info import ADMINS, SESSION_SWITCH_LIMIT
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.errors.exceptions.bad_request_400 import (
    ChannelInvalid,
    ChatAdminRequired,
    UsernameInvalid,
    UsernameNotModified
)
from info import INDEX_REQ_CHANNEL as LOG_CHANNEL
from database.ia_filterdb import save_file
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
lock = asyncio.Lock()


def _get_user_clients():
    """Returns the list of started user (session string) clients, if any."""
    clients = getattr(temp, 'USER_CLIENTS', None)
    if clients:
        return clients

    # Backward-compat: only a single USER_CLIENT was set.
    single = getattr(temp, 'USER_CLIENT', None)
    return [single] if single is not None else []


async def get_index_client(bot, chat_id=None):
    """
    Picks the best available client for indexing.

    - With one or more SESSION_STRINGs configured, and a chat_id given,
      each user session is tried in turn and the first one that can
      actually access that chat is used (a channel may only be joined
      by some of the accounts).
    - With no chat_id given (client not needed for an access check yet),
      user sessions are used round-robin so indexing load is spread
      across accounts.
    - Falls back to the bot client if no user session works / is set.
    """
    users = _get_user_clients()

    if not users:
        logger.info("Using BOT CLIENT for indexing")
        return bot

    if chat_id is not None:
        for i, user in enumerate(users):
            try:
                await user.get_chat(chat_id)
                logger.info(
                    "Using USER CLIENT #%s for indexing chat %s", i + 1, chat_id
                )
                return user
            except Exception:
                continue

        logger.info(
            "No user client has access to %s, falling back to BOT CLIENT",
            chat_id
        )
        return bot

    # No chat specified yet — round robin among available user clients.
    rr = getattr(temp, 'INDEX_CLIENT_RR', 0) % len(users)
    temp.INDEX_CLIENT_RR = rr + 1
    logger.info("Using USER CLIENT #%s for indexing (round-robin)", rr + 1)
    return users[rr]


async def iter_messages_compat(client, chat_id, limit=None, min_message_id=None, offset_id=0):
    """
    Compatibility message iterator.

    Some Pyrogram Client objects expose iter_messages(),
    while others expose get_chat_history().

    This function automatically uses whichever API exists.

    offset_id lets iteration resume partway through a chat's history —
    used when rotating to a different account mid-index so the new
    account picks up right after the last message the previous one saw.
    """

    # Standard Pyrogram / compatible client
    if hasattr(client, "iter_messages"):
        logger.info(
            "Indexing with %s.iter_messages()",
            type(client).__name__
        )

        async for message in client.iter_messages(
            chat_id,
            limit=limit,
            offset_id=offset_id
        ):
            if min_message_id is not None and message.id < min_message_id:
                break

            yield message

        return

    # Fallback for clients without iter_messages()
    if hasattr(client, "get_chat_history"):
        logger.info(
            "Client %s has no iter_messages(); "
            "using get_chat_history() instead",
            type(client).__name__
        )

        count = 0

        async for message in client.get_chat_history(
            chat_id=chat_id,
            limit=limit,
            offset_id=offset_id
        ):
            if min_message_id is not None and message.id < min_message_id:
                break

            yield message

            count += 1

            if limit is not None and count >= limit:
                break

        return

    raise AttributeError(
        f"Telegram client {type(client).__name__!r} "
        "has neither 'iter_messages' nor 'get_chat_history'"
    )


async def iter_messages_rotating_clients(
    clients, chat_id, limit, min_message_id, quota
):
    """
    Iterates a chat's messages for indexing, spread across one or more
    clients. After `quota` files have been confirmed SAVED on the
    current client, it rotates to the next client and resumes right
    after the last message seen — so no single account has to make
    all the API calls for a big channel and risk a FloodWait.

    This is an async generator you drive manually with asend():

        gen = iter_messages_rotating_clients(...)
        saved = None
        while True:
            try:
                message, active_client = await gen.asend(saved)
            except StopAsyncIteration:
                break
            ... process message, save/skip it ...
            saved = True  # only when it was newly saved as a file, else False

    If a client errors out partway through (e.g. it isn't a member of
    the chat, or gets kicked), it's skipped and the next one is tried
    from the same position; if every client fails, the error from the
    last one propagates.
    """
    if not clients:
        return

    offset_id = 0
    client_idx = 0

    while True:
        client = clients[client_idx % len(clients)]
        files_this_client = 0
        last_seen_id = None
        rotated = False
        failed_error = None

        logger.info(
            "Indexing chat %s with account #%s/%s (resuming after msg %s)",
            chat_id, client_idx + 1, len(clients), offset_id
        )

        try:
            async for message in iter_messages_compat(
                client,
                chat_id,
                limit=limit,
                min_message_id=min_message_id,
                offset_id=offset_id
            ):
                last_seen_id = message.id

                saved = yield message, client

                if quota and saved:
                    files_this_client += 1

                    if len(clients) > 1 and files_this_client >= quota:
                        logger.info(
                            "Account #%s hit its %s-file quota, "
                            "rotating to the next account",
                            client_idx + 1, quota
                        )
                        rotated = True
                        break

        except GeneratorExit:
            raise

        except Exception as e:
            logger.warning(
                "Account #%s errored while indexing %s: %s",
                client_idx + 1, chat_id, e
            )
            failed_error = e

        if last_seen_id is not None:
            offset_id = last_seen_id

        if rotated:
            client_idx += 1
            continue

        if failed_error is not None:
            if client_idx + 1 < len(clients):
                client_idx += 1
                continue
            raise failed_error

        # This client's iterator ran out naturally (hit min_message_id
        # or the end of the chat's history) — nothing left to index.
        break


@Client.on_callback_query(filters.regex(r'^index'))
async def index_files(bot, query):

    if query.data.startswith('index_cancel'):
        temp.CANCEL = True
        return await query.answer("Cancelling Indexing")

    _, raju, chat, lst_msg_id, from_user = query.data.split("#")

    if raju == 'reject':
        await query.message.delete()

        await bot.send_message(
            int(from_user),
            f'Your Submission for indexing {chat} has been declined by our moderators.',
            reply_to_message_id=int(lst_msg_id)
        )

        return

    if lock.locked():
        return await query.answer(
            'Wait until previous process complete.',
            show_alert=True
        )

    msg = query.message

    await query.answer(
        'Processing...⏳',
        show_alert=True
    )

    if int(from_user) not in ADMINS:
        await bot.send_message(
            int(from_user),
            f'Your Submission for indexing {chat} has been accepted by our moderators and will be added soon.',
            reply_to_message_id=int(lst_msg_id)
        )

    await msg.edit(
        "Starting Indexing",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    'Cancel',
                    callback_data='index_cancel'
                )
            ]]
        )
    )

    try:
        chat = int(chat)
    except Exception:
        pass

    # Use a USER_CLIENT that can access this chat, if any is available.
    index_client = await get_index_client(bot, chat_id=chat)

    # Run indexing in background.
    asyncio.create_task(
        index_files_to_db(
            int(lst_msg_id),
            chat,
            msg,
            index_client
        )
    )


@Client.on_message(filters.private & filters.command('index'))
async def send_for_index(bot, message):

    vj = await bot.ask(
        message.chat.id,
        "**Now Send Me Your Channel Last Post Link Or Forward A Last Message From Your Index Channel.\n\n"
        "And You Can Set Skip Number By - /setskip yourskipnumber**"
    )

    if (
        vj.forward_from_chat
        and vj.forward_from_chat.type == enums.ChatType.CHANNEL
    ):

        last_msg_id = vj.forward_from_message_id

        chat_id = (
            vj.forward_from_chat.username
            or vj.forward_from_chat.id
        )

    elif vj.text:

        regex = re.compile(
            r"(https://)?"
            r"(t\.me/|telegram\.me/|telegram\.dog/)"
            r"(c/)?"
            r"(\d+|[a-zA-Z_0-9]+)/"
            r"(\d+)$"
        )

        match = regex.match(vj.text)

        if not match:
            return await vj.reply(
                'Invalid link\n\nTry again by /index'
            )

        chat_id = match.group(4)
        last_msg_id = int(match.group(5))

        if chat_id.isnumeric():
            chat_id = int("-100" + chat_id)

    else:
        return

    # Pick whichever client (user session or bot) can access this chat.
    index_client = await get_index_client(bot, chat_id=chat_id)

    # Check channel access.
    try:

        await index_client.get_chat(chat_id)

    except ChannelInvalid:

        return await vj.reply(
            'Cannot access this channel. '
            'Make sure your account (or bot) is a member.'
        )

    except (
        UsernameInvalid,
        UsernameNotModified
    ):

        return await vj.reply(
            'Invalid Link specified.'
        )

    except Exception as e:

        logger.exception(e)

        return await vj.reply(
            f'Errors - {e}'
        )

    # Check last message.
    try:

        k = await index_client.get_messages(
            chat_id,
            last_msg_id
        )

    except Exception:

        return await message.reply(
            'Cannot read messages from this channel. '
            'Make sure your account is a member.'
        )

    if k.empty:

        return await message.reply(
            'This may be a group and access is restricted.'
        )

    if message.from_user.id in ADMINS:

        client_note = (
            "👤 Using your account (user session)"
            if index_client is not bot
            else
            "🤖 Using bot client"
        )

        buttons = [[
            InlineKeyboardButton(
                'Yes',
                callback_data=(
                    f'index#accept#'
                    f'{chat_id}#'
                    f'{last_msg_id}#'
                    f'{message.from_user.id}'
                )
            )
        ], [
            InlineKeyboardButton(
                'close',
                callback_data='close_data'
            )
        ]]

        reply_markup = InlineKeyboardMarkup(buttons)

        return await message.reply(
            f'Do you Want To Index This Channel/Group?\n\n'
            f'Chat ID/Username: <code>{chat_id}</code>\n'
            f'Last Message ID: <code>{last_msg_id}</code>\n\n'
            f'{client_note}',
            reply_markup=reply_markup
        )

    if type(chat_id) is int:

        try:

            link = (
                await index_client
                .create_chat_invite_link(chat_id)
            ).invite_link

        except ChatAdminRequired:

            try:

                chat_info = await index_client.get_chat(
                    chat_id
                )

                link = (
                    f"https://t.me/{chat_info.username}"
                    if chat_info.username
                    else
                    "Private Channel"
                )

            except Exception:

                link = "Private Channel"

    else:

        link = f"@{chat_id}"

    buttons = [[
        InlineKeyboardButton(
            'Accept Index',
            callback_data=(
                f'index#accept#'
                f'{chat_id}#'
                f'{last_msg_id}#'
                f'{message.from_user.id}'
            )
        )
    ], [
        InlineKeyboardButton(
            'Reject Index',
            callback_data=(
                f'index#reject#'
                f'{chat_id}#'
                f'{message.id}#'
                f'{message.from_user.id}'
            )
        )
    ]]

    reply_markup = InlineKeyboardMarkup(buttons)

    await bot.send_message(
        LOG_CHANNEL,

        f'#IndexRequest\n\n'
        f'By : {message.from_user.mention} '
        f'(<code>{message.from_user.id}</code>)\n'
        f'Chat ID/Username - <code>{chat_id}</code>\n'
        f'Last Message ID - <code>{last_msg_id}</code>\n'
        f'InviteLink - {link}',

        reply_markup=reply_markup
    )

    await message.reply(
        'ThankYou For the Contribution, '
        'Wait For My Moderators to verify the files.'
    )


@Client.on_message(
    filters.command('setskip') & filters.user(ADMINS)
)
async def set_skip_number(bot, message):

    if ' ' in message.text:

        _, skip = message.text.split(" ")

        try:
            skip = int(skip)

        except Exception:

            return await message.reply(
                "Skip number should be an integer."
            )

        await message.reply(
            f"Successfully set SKIP number as {skip}"
        )

        temp.CURRENT = int(skip)

    else:

        await message.reply(
            "Give me a skip number"
        )


async def index_files_to_db(
    lst_msg_id,
    chat,
    msg,
    client
):
    """
    Index files, spreading the work across all configured user
    sessions (SESSION_STRING / SESSION_STRING1, 2, 3, ...).

    Rotates to the next account every SESSION_SWITCH_LIMIT files so a
    single account never makes enough calls to trip a FloodWait.
    `client` (usually the bot, or whichever session was already
    confirmed to have access) is only used as a fallback when no
    SESSION_STRINGs are configured at all.
    """

    total_files = 0
    duplicate = 0
    errors = 0
    deleted = 0
    no_media = 0
    unsupported = 0

    async with lock:

        try:

            current = temp.CURRENT

            temp.CANCEL = False

            clients = _get_user_clients() or [client]
            quota = SESSION_SWITCH_LIMIT if len(clients) > 1 else 0

            if quota:
                logger.info(
                    "Multi-account indexing: %s accounts, "
                    "rotating every %s files",
                    len(clients), quota
                )

            # IMPORTANT:
            # Do NOT call client.iter_messages() directly.
            # Use the rotating compatibility iterator so, when multiple
            # SESSION_STRINGs are configured, load is spread across
            # accounts instead of hammering a single one.
            rotator = iter_messages_rotating_clients(
                clients,
                chat,
                limit=lst_msg_id,
                min_message_id=temp.CURRENT,
                quota=quota
            )

            saved_flag = None
            accounts_used = 1

            while True:

                try:
                    message, active_client = await rotator.asend(saved_flag)
                except StopAsyncIteration:
                    break

                saved_flag = False

                accounts_used = max(
                    accounts_used,
                    clients.index(active_client) + 1
                    if active_client in clients else accounts_used
                )

                if temp.CANCEL:

                    await rotator.aclose()

                    await msg.edit(
                        f"Successfully Cancelled!!\n\n"
                        f"Saved <code>{total_files}</code> files to dataBase!\n"
                        f"Duplicate Files Skipped: <code>{duplicate}</code>\n"
                        f"Deleted Messages Skipped: <code>{deleted}</code>\n"
                        f"Non-Media messages skipped: "
                        f"<code>{no_media + unsupported}</code> "
                        f"(Unsupported Media - `{unsupported}` )\n"
                        f"Errors Occurred: <code>{errors}</code>"
                    )

                    break

                current += 1

                # Yield control so other users stay responsive.
                await asyncio.sleep(0)

                # Extra breathing room every 5 messages.
                if current % 5 == 0:
                    await asyncio.sleep(0.05)

                # Update progress every 30 messages.
                if current % 30 == 0:

                    can = [[
                        InlineKeyboardButton(
                            'Cancel',
                            callback_data='index_cancel'
                        )
                    ]]

                    reply = InlineKeyboardMarkup(can)

                    try:

                        await msg.edit_text(
                            text=(
                                f"Total messages fetched: "
                                f"<code>{current}</code>\n"

                                f"Total messages saved: "
                                f"<code>{total_files}</code>\n"

                                f"Duplicate Files Skipped: "
                                f"<code>{duplicate}</code>\n"

                                f"Deleted Messages Skipped: "
                                f"<code>{deleted}</code>\n"

                                f"Non-Media messages skipped: "
                                f"<code>{no_media + unsupported}</code> "
                                f"(Unsupported Media - "
                                f"`{unsupported}` )\n"

                                f"Errors Occurred: "
                                f"<code>{errors}</code>"
                                + (
                                    f"\n\nAccounts used so far: "
                                    f"<code>{accounts_used}/{len(clients)}</code>"
                                    if quota else ""
                                )
                            ),

                            reply_markup=reply
                        )

                    except MessageNotModified:

                        pass

                # Deleted/empty message.
                if message.empty:

                    deleted += 1

                    continue

                # No media.
                elif not message.media:

                    no_media += 1

                    continue

                # Unsupported media.
                elif message.media not in [
                    enums.MessageMediaType.VIDEO,
                    enums.MessageMediaType.AUDIO,
                    enums.MessageMediaType.DOCUMENT
                ]:

                    unsupported += 1

                    continue

                media = getattr(
                    message,
                    message.media.value,
                    None
                )

                if not media:

                    unsupported += 1

                    continue

                media.caption = message.caption

                aynav, vnay = await save_file(
                    media
                )

                await asyncio.sleep(0)

                if aynav:

                    total_files += 1
                    saved_flag = True

                elif vnay == 0:

                    duplicate += 1

                elif vnay == 2:

                    errors += 1

        except Exception as e:

            logger.exception(e)

            k = await msg.edit(
                f'Error: {e}'
            )

            await k.reply_text(
                f'Saved <code>{total_files}</code> to dataBase!\n'
                f'Duplicate Files Skipped: '
                f'<code>{duplicate}</code>\n'
                f'Deleted Messages Skipped: '
                f'<code>{deleted}</code>\n'
                f'Non-Media messages skipped: '
                f'<code>{no_media + unsupported}</code> '
                f'(Unsupported Media - `{unsupported}` )\n'
                f'Errors Occurred: '
                f'<code>{errors}</code>'
            )

        else:

            await msg.edit(
                f'Successfully saved '
                f'<code>{total_files}</code> files to dataBase!\n'
                f'Duplicate Files Skipped: '
                f'<code>{duplicate}</code>\n'
                f'Deleted Messages Skipped: '
                f'<code>{deleted}</code>\n'
                f'Non-Media messages skipped: '
                f'<code>{no_media + unsupported}</code> '
                f'(Unsupported Media - `{unsupported}` )\n'
                f'Errors Occurred: '
                f'<code>{errors}</code>'
            )
