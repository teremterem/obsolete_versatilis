"""
A MiniAgent that is connected to a Telegram bot.
"""

import asyncio
import logging
from functools import partial

import telegram.error
from miniagents.messages import Message
from miniagents.miniagents import miniagent, InteractionContext
from miniagents.promising.sentinels import AWAIT
from miniagents.utils import achain_loop, split_messages
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder

from miniagents_copilot.agents.versatilis_agents import soul_crusher, history_agent
from versatilis_config import TELEGRAM_TOKEN

logger = logging.getLogger(__name__)

telegram_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

active_chats: dict[int, asyncio.Queue] = {}


@miniagent
async def telegram_update_agent(ctx: InteractionContext) -> None:
    """
    MiniAgent that receives Telegram updates from the webhook.
    """
    # noinspection PyBroadException
    try:
        async for message_promise in ctx.messages:
            message = await message_promise
            update: Update = Update.de_json(message.model_dump(), telegram_app.bot)
            await process_telegram_update(update)
    except Exception:  # pylint: disable=broad-except
        logger.exception("ERROR PROCESSING A TELEGRAM UPDATE")


async def process_telegram_update(update: Update) -> None:
    """
    Process a Telegram update.
    """
    if not update.effective_message or not update.effective_message.text:
        return

    if update.edited_message:
        # TODO Oleksandr: update the history when messages are edited
        return

    if update.effective_message.text == "/start":
        if update.effective_chat.id not in active_chats:
            # Start a conversation if it is not already started.
            # The following function will not return until the conversation is over (and it is never over :D)
            active_chats[update.effective_chat.id] = asyncio.Queue()
            try:
                await achain_loop(
                    agents=[
                        history_agent,
                        AWAIT,
                        soul_crusher,
                        echo_to_console,
                        partial(user_agent.inquire, telegram_chat_id=update.effective_chat.id),
                    ],
                )
            except Exception as exc:  # pylint: disable=broad-except
                logger.exception("ERROR IN THE CONVERSATION LOOP")
                await update.effective_chat.send_message("Sorry, something went wrong 🤖")
                await update.effective_chat.send_message(str(exc))

        return

    if update.effective_chat.id not in active_chats:
        # conversation is not started yet
        return

    queue = active_chats[update.effective_chat.id]
    await queue.put(update.effective_message.text)


@miniagent
async def echo_to_console(ctx: InteractionContext) -> None:
    """
    MiniAgent that echoes messages to the console token by token.
    """
    ctx.reply(ctx.messages)  # return the messages as they are
    async for message_promise in ctx.messages:
        async for token in message_promise:
            print(f"\033[92;1m{token}\033[0m", end="", flush=True)


@miniagent
async def user_agent(ctx: InteractionContext, telegram_chat_id: int) -> None:
    """
    This is a proxy agent that represents the user in the conversation loop. It is also responsible for maintaining
    the chat history.
    """
    async for message_promise in split_messages(ctx.messages, role="assistant"):
        await telegram_app.bot.send_chat_action(telegram_chat_id, "typing")

        # it's ok to sleep asynchronously, because the message tokens will be collected in the background anyway,
        # thanks to the way `MiniAgents` (or, more specifically, `promising`) framework is designed
        await asyncio.sleep(1)

        message = await message_promise
        if str(message).strip():
            try:
                await telegram_app.bot.send_message(
                    chat_id=telegram_chat_id, text=str(message), parse_mode=ParseMode.MARKDOWN
                )
            except telegram.error.BadRequest:
                await telegram_app.bot.send_message(chat_id=telegram_chat_id, text=str(message))

            ctx.reply(message)

    chat_queue = active_chats[telegram_chat_id]
    ctx.reply(await chat_queue.get())
    try:
        # let's give the user a chance to send a follow-up if they forgot something
        ctx.reply(await asyncio.wait_for(chat_queue.get(), timeout=3))
        while True:
            # if they did actually send a follow-up, then let's wait for a bit longer
            ctx.reply(await asyncio.wait_for(chat_queue.get(), timeout=15))
    except asyncio.TimeoutError:
        # if timeout happens we just finish the function - the user is done sending messages and is waiting for a
        # response from the Versatilis agent
        pass


class TelegramUpdateMessage(Message):
    """
    Telegram update MiniAgent message.
    """
