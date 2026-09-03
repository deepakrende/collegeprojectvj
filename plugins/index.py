# Don't Remove Credit @VJ_Bots
# Subscribe YouTube Channel For Amazing Bot @Tech_VJ
# Ask Doubt on telegram @KingVJ01

import logging, re, asyncio
from utils import temp
from info import ADMINS
from pyrogram import Client, filters, enums
from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.errors.exceptions.bad_request_400 import ChannelInvalid, ChatAdminRequired, UsernameInvalid, UsernameNotModified
from info import INDEX_REQ_CHANNEL as LOG_CHANNEL
from database.ia_filterdb import save_file
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
lock = asyncio.Lock()


def get_index_client(bot):
    """
    Returns user client if SESSION_STRING is set and client is available,
    otherwise falls back to bot client.
    User client can access channels where bot is not admin/member.
    """
    user = getattr(temp, 'USER_CLIENT', None)
    if user is not None:
        logger.info("Using USER CLIENT for indexing")
        return user
    logger.info("Using BOT CLIENT for indexing")
    return bot


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
        return await query.answer('Wait until previous process complete.', show_alert=True)
    msg = query.message

    await query.answer('Processing...⏳', show_alert=True)
    if int(from_user) not in ADMINS:
        await bot.send_message(
            int(from_user),
            f'Your Submission for indexing {chat} has been accepted by our moderators and will be added soon.',
            reply_to_message_id=int(lst_msg_id)
        )
    await msg.edit(
        "Starting Indexing",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton('Cancel', callback_data='index_cancel')]]
        )
    )
    try:
        chat = int(chat)
    except:
        chat = chat

    # ✅ Run indexing as background task — bot stays responsive
    # ✅ Uses USER CLIENT if SESSION_STRING is set, else uses bot client
    index_client = get_index_client(bot)
    asyncio.create_task(index_files_to_db(int(lst_msg_id), chat, msg, index_client))


@Client.on_message(filters.private & filters.command('index'))
async def send_for_index(bot, message):
    # ✅ Use user client if available for checking channel access
    index_client = get_index_client(bot)

    vj = await bot.ask(message.chat.id, "**Now Send Me Your Channel Last Post Link Or Forward A Last Message From Your Index Channel.\n\nAnd You Can Set Skip Number By - /setskip yourskipnumber**")
    if vj.forward_from_chat and vj.forward_from_chat.type == enums.ChatType.CHANNEL:
        last_msg_id = vj.forward_from_message_id
        chat_id = vj.forward_from_chat.username or vj.forward_from_chat.id
    elif vj.text:
        regex = re.compile("(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$")
        match = regex.match(vj.text)
        if not match:
            return await vj.reply('Invalid link\n\nTry again by /index')
        chat_id = match.group(4)
        last_msg_id = int(match.group(5))
        if chat_id.isnumeric():
            chat_id = int(("-100" + chat_id))
    else:
        return

    # ✅ Try with user client first, then fall back to bot
    try:
        await index_client.get_chat(chat_id)
    except ChannelInvalid:
        return await vj.reply('Cannot access this channel. Make sure your account (or bot) is a member.')
    except (UsernameInvalid, UsernameNotModified):
        return await vj.reply('Invalid Link specified.')
    except Exception as e:
        logger.exception(e)
        return await vj.reply(f'Errors - {e}')

    try:
        k = await index_client.get_messages(chat_id, last_msg_id)
    except:
        return await message.reply('Cannot read messages from this channel. Make sure your account is a member.')
    if k.empty:
        return await message.reply('This may be a group and access is restricted.')

    if message.from_user.id in ADMINS:
        # ✅ Show which client will be used
        client_note = "👤 Using your account (user session)" if getattr(temp, 'USER_CLIENT', None) else "🤖 Using bot client"
        buttons = [[
            InlineKeyboardButton('Yes', callback_data=f'index#accept#{chat_id}#{last_msg_id}#{message.from_user.id}')
        ],[
            InlineKeyboardButton('close', callback_data='close_data')
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
            link = (await index_client.create_chat_invite_link(chat_id)).invite_link
        except ChatAdminRequired:
            try:
                chat_info = await index_client.get_chat(chat_id)
                link = f"https://t.me/{chat_info.username}" if chat_info.username else "Private Channel"
            except:
                link = "Private Channel"
    else:
        link = f"@{chat_id}"

    buttons = [[
        InlineKeyboardButton('Accept Index', callback_data=f'index#accept#{chat_id}#{last_msg_id}#{message.from_user.id}')
    ],[
        InlineKeyboardButton('Reject Index', callback_data=f'index#reject#{chat_id}#{message.id}#{message.from_user.id}'),
    ]]
    reply_markup = InlineKeyboardMarkup(buttons)
    await bot.send_message(
        LOG_CHANNEL,
        f'#IndexRequest\n\nBy : {message.from_user.mention} (<code>{message.from_user.id}</code>)\nChat ID/Username - <code>{chat_id}</code>\nLast Message ID - <code>{last_msg_id}</code>\nInviteLink - {link}',
        reply_markup=reply_markup
    )
    await message.reply('ThankYou For the Contribution, Wait For My Moderators to verify the files.')


@Client.on_message(filters.command('setskip') & filters.user(ADMINS))
async def set_skip_number(bot, message):
    if ' ' in message.text:
        _, skip = message.text.split(" ")
        try:
            skip = int(skip)
        except:
            return await message.reply("Skip number should be an integer.")
        await message.reply(f"Successfully set SKIP number as {skip}")
        temp.CURRENT = int(skip)
    else:
        await message.reply("Give me a skip number")


async def index_files_to_db(lst_msg_id, chat, msg, client):
    """
    Index files using whichever client is passed —
    user client (can access private channels where user is member)
    or bot client (needs admin access).
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
            async for message in client.iter_messages(chat, lst_msg_id, temp.CURRENT):
                if temp.CANCEL:
                    await msg.edit(
                        f"Successfully Cancelled!!\n\nSaved <code>{total_files}</code> files to dataBase!\n"
                        f"Duplicate Files Skipped: <code>{duplicate}</code>\n"
                        f"Deleted Messages Skipped: <code>{deleted}</code>\n"
                        f"Non-Media messages skipped: <code>{no_media + unsupported}</code> "
                        f"(Unsupported Media - `{unsupported}` )\n"
                        f"Errors Occurred: <code>{errors}</code>"
                    )
                    break

                current += 1

                # Yield control every iteration so other users stay responsive
                await asyncio.sleep(0)

                # Extra breathing room every 5 messages
                if current % 5 == 0:
                    await asyncio.sleep(0.05)

                if current % 30 == 0:
                    can = [[InlineKeyboardButton('Cancel', callback_data='index_cancel')]]
                    reply = InlineKeyboardMarkup(can)
                    try:
                        await msg.edit_text(
                            text=(
                                f"Total messages fetched: <code>{current}</code>\n"
                                f"Total messages saved: <code>{total_files}</code>\n"
                                f"Duplicate Files Skipped: <code>{duplicate}</code>\n"
                                f"Deleted Messages Skipped: <code>{deleted}</code>\n"
                                f"Non-Media messages skipped: <code>{no_media + unsupported}</code> "
                                f"(Unsupported Media - `{unsupported}` )\n"
                                f"Errors Occurred: <code>{errors}</code>"
                            ),
                            reply_markup=reply
                        )
                    except MessageNotModified:
                        pass

                if message.empty:
                    deleted += 1
                    continue
                elif not message.media:
                    no_media += 1
                    continue
                elif message.media not in [enums.MessageMediaType.VIDEO, enums.MessageMediaType.AUDIO, enums.MessageMediaType.DOCUMENT]:
                    unsupported += 1
                    continue
                media = getattr(message, message.media.value, None)
                if not media:
                    unsupported += 1
                    continue
                media.caption = message.caption

                aynav, vnay = await save_file(media)
                await asyncio.sleep(0)

                if aynav:
                    total_files += 1
                elif vnay == 0:
                    duplicate += 1
                elif vnay == 2:
                    errors += 1

        except Exception as e:
            logger.exception(e)
            k = await msg.edit(f'Error: {e}')
            await k.reply_text(
                f'Saved <code>{total_files}</code> to dataBase!\n'
                f'Duplicate Files Skipped: <code>{duplicate}</code>\n'
                f'Deleted Messages Skipped: <code>{deleted}</code>\n'
                f'Non-Media messages skipped: <code>{no_media + unsupported}</code> '
                f'(Unsupported Media - `{unsupported}` )\n'
                f'Errors Occurred: <code>{errors}</code>'
            )
        else:
            await msg.edit(
                f'Successfully saved <code>{total_files}</code> to dataBase!\n'
                f'Duplicate Files Skipped: <code>{duplicate}</code>\n'
                f'Deleted Messages Skipped: <code>{deleted}</code>\n'
                f'Non-Media messages skipped: <code>{no_media + unsupported}</code> '
                f'(Unsupported Media - `{unsupported}` )\n'
                f'Errors Occurred: <code>{errors}</code>'
            )
