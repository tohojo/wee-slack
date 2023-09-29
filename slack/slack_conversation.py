from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import TYPE_CHECKING, Dict, List, Mapping, NoReturn, Optional, Union

import weechat

from slack.shared import shared
from slack.slack_message import SlackMessage, SlackTs
from slack.task import gather, run_async
from slack.util import get_callback_name

if TYPE_CHECKING:
    from slack_api.slack_conversations_info import SlackConversationsInfo
    from slack_rtm.slack_rtm_message import (
        SlackMessageChanged,
        SlackMessageDeleted,
        SlackMessageReplied,
        SlackShRoomJoin,
        SlackShRoomUpdate,
    )
    from typing_extensions import Literal

    from slack.slack_api import SlackApi
    from slack.slack_workspace import SlackWorkspace


def get_conversation_from_buffer_pointer(
    buffer_pointer: str,
) -> Optional[SlackConversation]:
    for workspace in shared.workspaces.values():
        for conversation in workspace.open_conversations.values():
            if conversation.buffer_pointer == buffer_pointer:
                return conversation
    return None


def invalidate_nicklists():
    for workspace in shared.workspaces.values():
        for conversation in workspace.open_conversations.values():
            conversation.nicklist_needs_refresh = True


def hdata_line_ts(line_pointer: str) -> Optional[SlackTs]:
    data = weechat.hdata_pointer(weechat.hdata_get("line"), line_pointer, "data")
    for i in range(
        weechat.hdata_integer(weechat.hdata_get("line_data"), data, "tags_count")
    ):
        tag = weechat.hdata_string(
            weechat.hdata_get("line_data"), data, f"{i}|tags_array"
        )
        if tag.startswith("slack_ts_"):
            return SlackTs(tag[9:])
    return None


def tags_set_notify_none(tags: List[str]) -> List[str]:
    notify_tags = {"notify_highlight", "notify_message", "notify_private"}
    tags = [tag for tag in tags if tag not in notify_tags]
    tags += ["no_highlight", "notify_none"]
    return tags


def modify_buffer_line(buffer_pointer: str, ts: SlackTs, new_text: str):
    own_lines = weechat.hdata_pointer(
        weechat.hdata_get("buffer"), buffer_pointer, "own_lines"
    )
    line_pointer = weechat.hdata_pointer(
        weechat.hdata_get("lines"), own_lines, "last_line"
    )

    # Find the last line with this ts
    is_last_line = True
    while line_pointer and hdata_line_ts(line_pointer) != ts:
        is_last_line = False
        line_pointer = weechat.hdata_move(weechat.hdata_get("line"), line_pointer, -1)

    if not line_pointer:
        return

    if shared.weechat_version >= 0x04000000:
        data = weechat.hdata_pointer(weechat.hdata_get("line"), line_pointer, "data")
        weechat.hdata_update(
            weechat.hdata_get("line_data"), data, {"message": new_text}
        )
        return

    # Find all lines for the message
    pointers: List[str] = []
    while line_pointer and hdata_line_ts(line_pointer) == ts:
        pointers.append(line_pointer)
        line_pointer = weechat.hdata_move(weechat.hdata_get("line"), line_pointer, -1)
    pointers.reverse()

    if not pointers:
        return

    if is_last_line:
        lines = new_text.split("\n")
        extra_lines_count = len(lines) - len(pointers)
        if extra_lines_count > 0:
            line_data = weechat.hdata_pointer(
                weechat.hdata_get("line"), pointers[0], "data"
            )
            tags_count = weechat.hdata_integer(
                weechat.hdata_get("line_data"), line_data, "tags_count"
            )
            tags = [
                weechat.hdata_string(
                    weechat.hdata_get("line_data"), line_data, f"{i}|tags_array"
                )
                for i in range(tags_count)
            ]
            tags = tags_set_notify_none(tags)
            tags_str = ",".join(tags)
            last_read_line = weechat.hdata_pointer(
                weechat.hdata_get("lines"), own_lines, "last_read_line"
            )
            should_set_unread = last_read_line == pointers[-1]

            # Insert new lines to match the number of lines in the message
            weechat.buffer_set(buffer_pointer, "print_hooks_enabled", "0")
            for _ in range(extra_lines_count):
                weechat.prnt_date_tags(buffer_pointer, ts.major, tags_str, " \t ")
                pointers.append(
                    weechat.hdata_pointer(
                        weechat.hdata_get("lines"), own_lines, "last_line"
                    )
                )
            if should_set_unread:
                weechat.buffer_set(buffer_pointer, "unread", "")
            weechat.buffer_set(buffer_pointer, "print_hooks_enabled", "1")
    else:
        # Split the message into at most the number of existing lines as we can't insert new lines
        lines = new_text.split("\n", len(pointers) - 1)
        # Replace newlines to prevent garbled lines in bare display mode
        lines = [line.replace("\n", " | ") for line in lines]

    # Extend lines in case the new message is shorter than the old as we can't delete lines
    lines += [""] * (len(pointers) - len(lines))

    for pointer, line in zip(pointers, lines):
        data = weechat.hdata_pointer(weechat.hdata_get("line"), pointer, "data")
        weechat.hdata_update(weechat.hdata_get("line_data"), data, {"message": line})


def sha1_hex(string: str) -> str:
    return str(hashlib.sha1(string.encode()).hexdigest())


def ts_hash(ts: SlackTs) -> str:
    return sha1_hex(str(ts))


class SlackConversationMessageHashes(Dict[SlackTs, str]):
    def __init__(self, conversation: SlackConversation):
        self._conversation = conversation
        self._inverse_map: Dict[str, SlackTs] = {}

    def __setitem__(self, key: SlackTs, value: str) -> NoReturn:
        raise RuntimeError("Set from outside isn't allowed")

    def __delitem__(self, key: SlackTs) -> None:
        if key in self:
            hash_key = self[key]
            del self._inverse_map[hash_key]
        super().__delitem__(key)

    def _setitem(self, key: SlackTs, value: str) -> None:
        super().__setitem__(key, value)

    def __missing__(self, key: SlackTs) -> str:
        hash_len = 3
        full_hash = ts_hash(key)
        short_hash = full_hash[:hash_len]

        while any(
            existing_hash.startswith(short_hash) for existing_hash in self._inverse_map
        ):
            hash_len += 1
            short_hash = full_hash[:hash_len]

        if short_hash[:-1] in self._inverse_map:
            ts_with_same_hash = self._inverse_map.pop(short_hash[:-1])
            other_full_hash = ts_hash(ts_with_same_hash)
            other_short_hash = other_full_hash[:hash_len]

            while short_hash == other_short_hash:
                hash_len += 1
                short_hash = full_hash[:hash_len]
                other_short_hash = other_full_hash[:hash_len]

            self._setitem(ts_with_same_hash, other_short_hash)
            self._inverse_map[other_short_hash] = ts_with_same_hash

            other_message = self._conversation.messages[ts_with_same_hash]
            run_async(self._conversation.rerender_message(other_message))
            for reply in other_message.replies:
                run_async(self._conversation.rerender_message(reply))

        self._setitem(key, short_hash)
        self._inverse_map[short_hash] = key
        return self[key]


class SlackConversation:
    def __init__(
        self,
        workspace: SlackWorkspace,
        info: SlackConversationsInfo,
    ):
        self.workspace = workspace
        self._info = info
        self._members: Optional[List[str]] = None
        self._messages: OrderedDict[SlackTs, SlackMessage] = OrderedDict()
        self._typing_self_last_sent = time.time()
        # TODO: buffer_pointer may be accessed by buffer_switch before it's initialized
        self.buffer_pointer: str = ""
        self.is_loading = False
        self.history_filled = False
        self.history_pending = False
        self.nicklist_needs_refresh = True
        self.message_hashes = SlackConversationMessageHashes(self)

        self.completion_context: Literal[
            "NO_COMPLETION",
            "PENDING_COMPLETION",
            "ACTIVE_COMPLETION",
            "IN_PROGRESS_COMPLETION",
        ] = "NO_COMPLETION"
        self.completion_values: List[str] = []
        self.completion_index = 0

    @classmethod
    async def create(cls, workspace: SlackWorkspace, conversation_id: str):
        info_response = await workspace.api.fetch_conversations_info(conversation_id)
        return cls(workspace, info_response["channel"])

    @property
    def _api(self) -> SlackApi:
        return self.workspace.api

    @property
    def id(self) -> str:
        return self._info["id"]

    @property
    def messages(self) -> Mapping[SlackTs, SlackMessage]:
        return self._messages

    @property
    def type(self) -> Literal["channel", "private", "mpim", "im"]:
        if self._info["is_im"] is True:
            return "im"
        elif self._info["is_mpim"] is True:
            return "mpim"
        elif self._info["is_private"] is True:
            return "private"
        else:
            return "channel"

    async def name(self) -> str:
        if self._info["is_im"] is True:
            im_user = await self.workspace.users[self._info["user"]]
            return im_user.nick()
        elif self._info["is_mpim"] is True:
            members = await self.load_members(load_all=True)
            member_users = await gather(
                *(self.workspace.users[user_id] for user_id in members)
            )
            return ",".join([user.nick() for user in member_users])
        else:
            return self._info["name"]

    def name_prefix(
        self,
        name_type: Literal["full_name", "short_name", "short_name_without_padding"],
    ) -> str:
        if self._info["is_im"] is True:
            if name_type == "short_name":
                return " "
            else:
                return ""
        elif self._info["is_mpim"]:
            if name_type == "short_name" or name_type == "short_name_without_padding":
                return "@"
            else:
                return ""
        else:
            return "#"

    async def name_with_prefix(
        self,
        name_type: Literal["full_name", "short_name", "short_name_without_padding"],
    ) -> str:
        return f"{self.name_prefix(name_type)}{await self.name()}"

    @contextmanager
    def loading(self):
        self.is_loading = True
        weechat.bar_item_update("input_text")
        try:
            yield
        finally:
            self.is_loading = False
            weechat.bar_item_update("input_text")

    @contextmanager
    def completing(self):
        self.completion_context = "IN_PROGRESS_COMPLETION"
        try:
            yield
        finally:
            self.completion_context = "ACTIVE_COMPLETION"

    async def open_if_open(self):
        if "is_open" in self._info:
            if self._info["is_open"]:
                await self.open_buffer()
        elif self._info.get("is_member"):
            await self.open_buffer()

    async def open_buffer(self):
        if self.buffer_pointer:
            return

        name = await self.name()
        name_with_prefix_for_full_name = f"{self.name_prefix('full_name')}{name}"
        full_name = f"{shared.SCRIPT_NAME}.{self.workspace.name}.{name_with_prefix_for_full_name}"
        short_name = self.name_prefix("short_name") + name

        buffer_props = {
            "short_name": short_name,
            "title": "topic",
            "input_multiline": "1",
            "nicklist": "0" if self.type == "im" else "1",
            "nicklist_display_groups": "0",
            "localvar_set_type": (
                "private" if self.type in ("im", "mpim") else "channel"
            ),
            "localvar_set_slack_type": self.type,
            "localvar_set_nick": self.workspace.my_user.nick(),
            "localvar_set_channel": name_with_prefix_for_full_name,
            "localvar_set_server": self.workspace.name,
        }

        if shared.weechat_version >= 0x03050000:
            self.buffer_pointer = weechat.buffer_new_props(
                full_name,
                buffer_props,
                get_callback_name(self._buffer_input_cb),
                "",
                get_callback_name(self._buffer_close_cb),
                "",
            )
        else:
            self.buffer_pointer = weechat.buffer_new(
                full_name,
                get_callback_name(self._buffer_input_cb),
                "",
                get_callback_name(self._buffer_close_cb),
                "",
            )
            for prop_name, value in buffer_props.items():
                weechat.buffer_set(self.buffer_pointer, prop_name, value)

        self.workspace.open_conversations[self.id] = self

    async def buffer_switched_to(self):
        await gather(self.nicklist_update(), self.fill_history())

    async def load_members(self, load_all: bool = False):
        if self._members is None:
            members_response = await self._api.fetch_conversations_members(
                self, pages=-1 if load_all else 1
            )
            self._members = members_response["members"]
        self.workspace.users.initialize_items(self._members)
        return self._members

    async def fill_history(self):
        if self.history_filled or self.history_pending:
            return

        with self.loading():
            self.history_pending = True

            history = await self._api.fetch_conversations_history(self)

            messages = [SlackMessage(self, message) for message in history["messages"]]
            for message in messages:
                self._messages[message.ts] = message

            # TODO: Account for reply messages

            sender_user_ids = [m.sender_user_id for m in messages if m.sender_user_id]
            self.workspace.users.initialize_items(sender_user_ids)

            sender_bot_ids = [m.sender_bot_id for m in messages if m.sender_bot_id]
            self.workspace.bots.initialize_items(sender_bot_ids)

            await gather(*(message.render() for message in messages))

            for message in reversed(messages):
                if self.should_display_message(message):
                    await self.print_message(message, backlog=True)

            self.history_filled = True
            self.history_pending = False

    async def nicklist_update(self):
        if self.nicklist_needs_refresh:
            self.nicklist_needs_refresh = False
            members = await self.load_members()
            weechat.nicklist_remove_all(self.buffer_pointer)
            await gather(*(self.nicklist_add_user(user_id) for user_id in members))

    async def nicklist_add_user(self, user_id: str):
        user = await self.workspace.users[user_id]
        # TODO: weechat.color.nicklist_away
        color = user.nick_color() if shared.config.look.color_nicks_in_nicklist else ""
        weechat.nicklist_add_nick(
            self.buffer_pointer, "", user.nick(), color, "", "", 1
        )

    def display_thread_replies(self) -> bool:
        buffer_value = weechat.buffer_get_string(
            self.buffer_pointer, "localvar_display_thread_replies_in_channel"
        )
        if buffer_value:
            return bool(weechat.config_string_to_boolean(buffer_value))
        return shared.config.look.display_thread_replies_in_channel.value

    def should_display_message(self, message: SlackMessage) -> bool:
        return (
            not message.is_reply
            or message.is_thread_broadcast
            or self.display_thread_replies()
        )

    async def add_new_message(self, message: SlackMessage):
        # TODO: Remove old messages
        self._messages[message.ts] = message

        if self.should_display_message(message):
            if self.history_filled:
                await self.print_message(message)
            else:
                weechat.buffer_set(
                    self.buffer_pointer, "hotlist", str(message.priority.value)
                )

        parent_message = message.parent_message
        if parent_message:
            parent_message.replies.append(message)

        if message.sender_user_id:
            # TODO: thread buffers
            user = await self.workspace.users[message.sender_user_id]
            weechat.hook_signal_send(
                "typing_set_nick",
                weechat.WEECHAT_HOOK_SIGNAL_STRING,
                f"{self.buffer_pointer};off;{user.nick()}",
            )

    async def rerender_message(self, message: SlackMessage):
        modify_buffer_line(
            self.buffer_pointer, message.ts, await message.render_message(rerender=True)
        )

    async def rerender_history(self):
        for message in self._messages.values():
            await self.rerender_message(message)

    async def change_message(
        self, data: Union[SlackMessageChanged, SlackMessageReplied]
    ):
        ts = SlackTs(data["ts"])
        message = self._messages.get(ts)
        if message:
            message.update_message_json(data["message"])
            await self.rerender_message(message)

    async def delete_message(self, data: SlackMessageDeleted):
        ts = SlackTs(data["deleted_ts"])
        if ts in self.message_hashes:
            del self.message_hashes[ts]
        message = self._messages.get(ts)
        if message:
            message.deleted = True
            await self.rerender_message(message)

    async def update_message_room(
        self, data: Union[SlackShRoomJoin, SlackShRoomUpdate]
    ):
        ts = SlackTs(data["room"]["thread_root_ts"])
        message = self._messages.get(ts)
        if message:
            message.update_message_json_room(data["room"])
            await self.rerender_message(message)

    async def reaction_add(self, message_ts: SlackTs, reaction: str, user_id: str):
        message = self._messages.get(message_ts)
        if message:
            message.reaction_add(reaction, user_id)
            await self.rerender_message(message)

    async def reaction_remove(self, message_ts: SlackTs, reaction: str, user_id: str):
        message = self._messages.get(message_ts)
        if message:
            message.reaction_remove(reaction, user_id)
            await self.rerender_message(message)

    async def typing_add_user(self, user_id: str, thread_ts: Optional[str]):
        if not shared.config.look.typing_status_nicks:
            return

        if not thread_ts:
            user = await self.workspace.users[user_id]
            weechat.hook_signal_send(
                "typing_set_nick",
                weechat.WEECHAT_HOOK_SIGNAL_STRING,
                f"{self.buffer_pointer};typing;{user.nick()}",
            )

    def typing_update_self(self, typing_state: str):
        now = time.time()
        if now - 4 > self._typing_self_last_sent:
            self._typing_self_last_sent = now
            self.workspace.send_typing(self.id)

    async def print_message(self, message: SlackMessage, backlog: bool = False):
        tags = await message.tags(backlog=backlog)
        rendered = await message.render()
        weechat.prnt_date_tags(self.buffer_pointer, message.ts.major, tags, rendered)

    def _buffer_input_cb(self, data: str, buffer: str, input_data: str) -> int:
        weechat.prnt(buffer, "Text: %s" % input_data)
        return weechat.WEECHAT_RC_OK

    def _buffer_close_cb(self, data: str, buffer: str) -> int:
        self.buffer_pointer = ""
        self.history_filled = False
        return weechat.WEECHAT_RC_OK
