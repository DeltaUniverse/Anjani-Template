"""Anjani base telegram"""
# Copyright (C) 2020 - 2022  UserbotIndo Team, <https://github.com/userbotindo.git>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import asyncio
import signal
import sys
from functools import partial
from hashlib import sha256
from typing import TYPE_CHECKING, Any, MutableMapping, Optional, Set, Tuple, Type, Union

import pyrogram.filters as flt
from aiopath import AsyncPath
from pyrogram.client import Client
from pyrogram.filters import Filter
from pyrogram.handlers.callback_query_handler import CallbackQueryHandler
from pyrogram.handlers.chat_member_updated_handler import ChatMemberUpdatedHandler
from pyrogram.handlers.inline_query_handler import InlineQueryHandler
from pyrogram.handlers.message_handler import MessageHandler
from pyrogram.types import CallbackQuery, InlineQuery, Message, User
from yaml import full_load

from anjani import util
from anjani.language import getLangFile

from .anjani_mixin_base import MixinBase

if TYPE_CHECKING:
    from .anjani_bot import Anjani

EventType = Union[CallbackQuery, InlineQuery, Message]
TgEventHandler = Union[
    CallbackQueryHandler, InlineQueryHandler, MessageHandler, ChatMemberUpdatedHandler
]


class TelegramBot(MixinBase):
    # Initialized during instantiation
    __running: bool
    _plugin_event_handlers: MutableMapping[str, Tuple[TgEventHandler, int]]

    loaded: bool
    staff: Set[int]
    devs: Set[int]
    chats_languages: MutableMapping[int, str]
    languages: MutableMapping[str, MutableMapping[str, str]]

    # Initialized during startup
    client: Client
    user: User
    uid: int
    start_time_us: int
    owner: int

    def __init__(self: "Anjani", **kwargs: Any) -> None:
        self.__running = False
        self._plugin_event_handlers = {}

        self.loaded = False
        self.staff = set()
        self.devs = set()
        self.chats_languages = {}
        self.languages = {}

        # Propagate initialization to other mixins
        super().__init__(**kwargs)

    async def init_client(self: "Anjani") -> None:
        api_id = int(self.config["api_id"])
        api_hash = self.config["api_hash"]
        bot_token = self.config["bot_token"]

        try:
            self.owner = int(self.config["owner_id"])
        except KeyError:
            self.log.warning("Owner id is not set! you won't be able to run staff command!")
            self.owner = 0

        # Load session from database
        data = await self.db.get_collection("SESSION").find_one(
            {"_id": sha256(str(api_id).encode()).hexdigest()}
        )
        file = AsyncPath("anjani/anjani.session")
        if data and not await file.exists():
            self.log.info("Loading session from database")
            await file.write_bytes(data["session"])

        # Initialize Telegram client with gathered parameters
        self.client = Client(
            name="anjani",
            api_id=api_id,
            api_hash=api_hash,
            bot_token=bot_token,
            workdir="anjani",
        )

    async def start(self: "Anjani") -> None:
        if self.__running:
            raise RuntimeError("This bot instance is already running")

        self.log.info("Starting")
        await self.init_client()

        # Register core command handler
        self.client.add_handler(MessageHandler(self.on_command, self.command_predicate()), -1)

        # Load plugin
        self.load_all_plugins()
        await self.dispatch_event("load")
        self.loaded = True

        # Start Telegram client
        try:
            await self.client.start()
        except AttributeError:
            self.log.error(
                "Unable to get input for authorization! Make sure all configuration are done before running the bot."
            )
            raise

        # Get info
        async with asyncio.Lock():  # Lock to avoid race condition with command_dispatcher
            user = await self.client.get_me()

        if not isinstance(user, User):
            raise TypeError("Missing full self user information")

        self.user = user
        # noinspection PyTypeChecker
        self.uid = user.id
        self.staff.add(self.owner)
        self.devs.add(self.owner)

        # Update staff from db
        async for doc in self.db.get_collection("STAFF").find():
            if doc["rank"] == "dev":
                self.devs.add(doc["_id"])

            self.staff.add(doc["_id"])

        # Update global staff variable
        util.tg.STAFF.update(self.staff)

        # Update Language setting chat from db
        async for data in self.db.get_collection("LANGUAGE").find():
            self.chats_languages[data["chat_id"]] = data["language"]

        # Load text from language file
        async for language_file in getLangFile():
            self.languages[language_file.stem] = await util.run_sync(
                full_load, await language_file.read_text()
            )

        # Record start time and dispatch start event
        self.start_time_us = util.time.usec()
        await self.dispatch_event("start", self.start_time_us)

        self.log.info("Bot is ready")

        self.log.info("Catching up on missed events")
        await self.dispatch_missed_events()
        self.log.info("Finished catching up")

        # Dispatch final late start event
        await self.dispatch_event("started")

    async def idle(self: "Anjani") -> None:
        if self.__running:
            raise RuntimeError("This bot instance is already running")

        signals = {
            k: v
            for v, k in signal.__dict__.items()
            if v.startswith("SIG") and not v.startswith("SIG_")
        }

        if sys.platform == "win32":

            def signal_handler_windows(signum: int, _: Any) -> None:
                print(flush=True)
                self.log.info(f"Stop signal received ('{signals[signum]}').")
                self.__running = False

            for signame in (signal.SIGINT, signal.SIGTERM, signal.SIGABRT):
                signal.signal(signame, signal_handler_windows)
        else:

            def signal_handler(signum: int) -> None:
                print(flush=True)  # Separate signal and next log
                self.log.info(f"Stop signal received ('{signals[signum]}').")
                self.__running = False

            for signame in (signal.SIGINT, signal.SIGTERM, signal.SIGABRT):
                self.loop.add_signal_handler(signame, partial(signal_handler, signame))

        self.__running = True
        while self.__running:
            await asyncio.sleep(1)

    async def run(self: "Anjani") -> None:
        if self.__running:
            raise RuntimeError("This bot instance is already running")

        try:
            # Start client
            try:
                await self.start()
            except KeyboardInterrupt:
                self.log.warning("Received interrupt while connecting")
                return

            # Request updates, then idle until disconnected
            await self.idle()
        finally:
            # Make sure we stop when done
            await self.stop()

    def update_plugin_event(
        self: "Anjani",
        name: str,
        event_type: Type[TgEventHandler],
        *,
        filters: Optional[Filter] = None,
        group: int = 0,
    ) -> None:
        if name in self.listeners:
            # Add if there ARE listeners and it's NOT already registered
            if name not in self._plugin_event_handlers:

                async def event_handler(
                    client: Client, event: EventType  # skipcq: PYL-W0613
                ) -> None:
                    await self.dispatch_event(name, event)

                if filters is not None:
                    handler_info = (event_type(event_handler, filters), group)
                else:
                    handler_info = (event_type(event_handler), group)

                self.client.add_handler(*handler_info)
                self._plugin_event_handlers[name] = handler_info
        elif name in self._plugin_event_handlers:
            # Remove if there are NO listeners and it's ALREADY registered
            self.client.remove_handler(*self._plugin_event_handlers[name])
            del self._plugin_event_handlers[name]

    def update_plugin_events(self: "Anjani") -> None:
        self.update_plugin_event("callback_query", CallbackQueryHandler)
        self.update_plugin_event(
            "chat_action", MessageHandler, filters=flt.new_chat_members | flt.left_chat_member
        )
        self.update_plugin_event("chat_member_update", ChatMemberUpdatedHandler)
        self.update_plugin_event("chat_migrate", MessageHandler, filters=flt.migrate_from_chat_id)
        self.update_plugin_event("inline_query", InlineQueryHandler)
        self.update_plugin_event(
            "message",
            MessageHandler,
            filters=~flt.new_chat_members
            & ~flt.left_chat_member
            & ~flt.migrate_from_chat_id
            & ~flt.migrate_to_chat_id,
        )

    @property
    def events_activated(self: "Anjani") -> int:
        return len(self._plugin_event_handlers)

    def redact_message(self: "Anjani", text: str) -> str:
        api_id = self.config["api_id"]
        api_hash = self.config["api_hash"]
        bot_token = self.config["bot_token"]
        db_uri = self.config["db_uri"]

        if api_id in text:
            text = text.replace(api_id, "[REDACTED]")
        if api_hash in text:
            text = text.replace(api_hash, "[REDACTED]")
        if bot_token in text:
            text = text.replace(bot_token, "[REDACTED]")
        if db_uri in text:
            text = text.replace(db_uri, "[REDACTED]")

        return text

    async def respond(
        self: "Anjani",
        msg: Message,
        text: str = "",
        *,
        mode: Optional[str] = "edit",
        redact: bool = True,
        response: Optional[Message] = None,
        **kwargs: Any,
    ) -> Message:
        async def reply(reference: Message, *, text: str = "", **kwargs: Any) -> Message:
            if animation := kwargs.pop("animation", None):
                return await reference.reply_animation(animation, caption=text, **kwargs)
            if audio := kwargs.pop("audio", None):
                return await reference.reply_audio(audio, caption=text, **kwargs)
            if document := kwargs.pop("document", None):
                return await reference.reply_document(document, caption=text, **kwargs)
            if photo := kwargs.pop("photo", None):
                return await reference.reply_photo(photo, caption=text, **kwargs)
            if video := kwargs.pop("video", None):
                return await reference.reply_video(video, caption=text, **kwargs)

            return await reference.reply(text, **kwargs)

        if text:
            # Redact sensitive information if enabled and known
            if redact:
                text = self.redact_message(text)
            # Truncate messages longer than Telegram's 4096-character length limit
            text = util.tg.truncate(text)

        # get rid of emtpy value "animation", "audio", "document", "photo", "video"
        for key, value in dict(kwargs).items():
            if key in {"animation", "audio", "document", "photo", "video"} and not value:
                del kwargs[key]

        # force reply and as default behaviour if response is None
        if mode == "reply" or response is None and mode == "edit":
            return await reply(msg, text=text, **kwargs)

        # Only accept edit if we already respond the original msg
        if response is not None and mode == "edit":
            if any(key in kwargs for key in ("animation", "audio", "document", "photo", "video")):
                # Make client re-send the message with the new media instead editing a text
                await response.delete()
                return await reply(msg, text=text or response.text, **kwargs)

            if "reply_to_message_id" in kwargs:
                del kwargs["reply_to_message_id"]

            return await response.edit(text, **kwargs)

        raise ValueError(f"Unknown response mode {mode}")
