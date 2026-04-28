"""Shared types for the telegram bot."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.enums import ChatType
from aiogram.types import Message

if TYPE_CHECKING:
    from telegram_bot.core.services.virtual_topics import VirtualTopicsStore

ChannelKey = tuple[int, int | None]
"""Compound key (chat_id, thread_id) identifying a unique conversation channel.

When thread_id is None, represents classic (non-topic) chat mode.
For private chats with an active virtual slot, thread_id is a synthetic
value >= VIRTUAL_OFFSET produced by VirtualTopicsStore — see virtual_topics.py.
"""


def channel_key(message: Message) -> ChannelKey:
    """Extract the bare ChannelKey from an aiogram Message.

    No virtual-slot resolution is performed here. Callers that should respect
    a user's active virtual slot in a private chat must use
    `resolve_channel_key` instead.
    """
    return (message.chat.id, message.message_thread_id)


def resolve_channel_key(
    message: Message,
    virtual_topics: VirtualTopicsStore | None = None,
) -> ChannelKey:
    """Like `channel_key`, but in private chats fall through to the user's active virtual slot.

    Behaviour:
      * Forum/supergroup messages with a real `message_thread_id` are returned
        unchanged — virtual topics never override forum topics.
      * Private chats look up the active virtual slot for `from_user.id`. If
        there is one, the synthetic thread_id is substituted into the key so
        all downstream state (topic_config, tmux session names, channel
        sessions, MCP routing) becomes per-slot automatically.
      * If no store is provided, or the user has no active slot, the original
        `(chat_id, None)` is returned — preserving the pre-virtual-topics
        behaviour for the default chat.
    """
    base = channel_key(message)
    if base[1] is not None:
        return base
    if virtual_topics is None:
        return base
    if message.chat.type != ChatType.PRIVATE:
        return base
    user = message.from_user
    if user is None:
        return base
    synthetic = virtual_topics.current_thread_id(user.id)
    if synthetic is None:
        return base
    return (base[0], synthetic)
