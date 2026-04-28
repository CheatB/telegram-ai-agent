"""Commands for managing virtual topics (per-user named project slots in private chats)."""

from __future__ import annotations

import html
import logging

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from telegram_bot.core.messages import t
from telegram_bot.core.services.virtual_topics import VirtualTopicsStore

logger = logging.getLogger(__name__)

router = Router(name="virtual_topics")


def _escape(name: str) -> str:
    """HTML-escape a slot name before embedding into the picker captions."""
    return html.escape(name, quote=False)


async def _reject_non_private(message: Message) -> bool:
    """Reply with a private-chat-only notice when invoked outside DMs.

    Returns True if the command was rejected (caller should stop), False to continue.
    """
    if message.chat.type == ChatType.PRIVATE:
        return False
    await message.answer(t("ui.vt_only_in_private"))
    return True


@router.message(Command("topics"))
async def handle_topics_list(
    message: Message,
    virtual_topics: VirtualTopicsStore,
) -> None:
    """List the caller's virtual topics with a marker for the active one."""
    if await _reject_non_private(message):
        return
    user = message.from_user
    if user is None:
        return
    slots = virtual_topics.list_slots(user.id)
    if not slots:
        await message.answer(t("ui.vt_list_empty"), parse_mode="HTML")
        return
    active = virtual_topics.current_slot(user.id)
    active_id = active.slot_id if active is not None else None
    active_marker = t("ui.vt_list_active_marker")
    idle_marker = t("ui.vt_list_idle_marker")
    lines = [t("ui.vt_list_header")]
    for slot in slots:
        marker = active_marker if slot.slot_id == active_id else idle_marker
        lines.append(f"{marker} <b>{_escape(slot.name)}</b>")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("current"))
async def handle_current(
    message: Message,
    virtual_topics: VirtualTopicsStore,
) -> None:
    """Show the active virtual topic (or a hint if none)."""
    if await _reject_non_private(message):
        return
    user = message.from_user
    if user is None:
        return
    active = virtual_topics.current_slot(user.id)
    if active is None:
        await message.answer(t("ui.vt_current_none"), parse_mode="HTML")
        return
    await message.answer(
        t("ui.vt_current_active", name=_escape(active.name)),
        parse_mode="HTML",
    )


@router.message(Command("topic_new"))
async def handle_topic_new(
    message: Message,
    command: CommandObject,
    virtual_topics: VirtualTopicsStore,
) -> None:
    """Create a new virtual topic and make it active."""
    if await _reject_non_private(message):
        return
    user = message.from_user
    if user is None:
        return
    raw = (command.args or "").strip().split()
    if not raw:
        await message.answer(t("ui.vt_usage_new"), parse_mode="HTML")
        return
    name = raw[0]
    try:
        slot = await virtual_topics.create_slot(user.id, name)
    except ValueError as exc:
        # `create_slot` raises for two distinct conditions; both reach the user
        # as friendly messages. Match by string content rather than introducing
        # a custom exception class for two cases.
        text = str(exc)
        if "already exists" in text:
            await message.answer(
                t("ui.vt_already_exists", name=_escape(name)),
                parse_mode="HTML",
            )
        else:
            await message.answer(t("ui.vt_invalid_name"))
        return
    await message.answer(
        t("ui.vt_created", name=_escape(slot.name)),
        parse_mode="HTML",
    )


@router.message(Command("topic_switch"))
async def handle_topic_switch(
    message: Message,
    command: CommandObject,
    virtual_topics: VirtualTopicsStore,
) -> None:
    """Switch active topic to one of the user's existing slots."""
    if await _reject_non_private(message):
        return
    user = message.from_user
    if user is None:
        return
    raw = (command.args or "").strip().split()
    if not raw:
        await message.answer(t("ui.vt_usage_switch"), parse_mode="HTML")
        return
    name = raw[0]
    slot = await virtual_topics.switch_to(user.id, name)
    if slot is None:
        await message.answer(
            t("ui.vt_not_found", name=_escape(name)),
            parse_mode="HTML",
        )
        return
    await message.answer(
        t("ui.vt_switched", name=_escape(slot.name)),
        parse_mode="HTML",
    )


@router.message(Command("topic_del"))
async def handle_topic_del(
    message: Message,
    command: CommandObject,
    virtual_topics: VirtualTopicsStore,
) -> None:
    """Delete one of the user's slots. The active selection rolls over if needed."""
    if await _reject_non_private(message):
        return
    user = message.from_user
    if user is None:
        return
    raw = (command.args or "").strip().split()
    if not raw:
        await message.answer(t("ui.vt_usage_del"), parse_mode="HTML")
        return
    name = raw[0]
    ok = await virtual_topics.delete_slot(user.id, name)
    if not ok:
        await message.answer(
            t("ui.vt_not_found", name=_escape(name)),
            parse_mode="HTML",
        )
        return
    new_active = virtual_topics.current_slot(user.id)
    if new_active is None:
        # No remaining slots — the user is back to the default (chat_id, None).
        await message.answer(
            t("ui.vt_deleted_no_active", name=_escape(name)),
            parse_mode="HTML",
        )
        return
    await message.answer(
        t(
            "ui.vt_deleted_now_active",
            name=_escape(name),
            active=_escape(new_active.name),
        ),
        parse_mode="HTML",
    )


@router.message(Command("topic_off"))
async def handle_topic_off(
    message: Message,
    virtual_topics: VirtualTopicsStore,
) -> None:
    """Deactivate the active virtual topic so the user falls back to the default chat."""
    if await _reject_non_private(message):
        return
    user = message.from_user
    if user is None:
        return
    cleared = await virtual_topics.deactivate(user.id)
    if cleared:
        await message.answer(t("ui.vt_off"))
    else:
        await message.answer(t("ui.vt_already_off"))
