"""Commands for managing virtual topics (per-user named project slots in private chats)."""

from __future__ import annotations

import html
import logging
import os

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from telegram_bot.core.messages import t
from telegram_bot.core.services.providers import engine_display_name
from telegram_bot.core.services.topic_config import TopicConfig
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
    topic_config: TopicConfig,
) -> None:
    """Create a new virtual topic, optionally bind it to a project path.

    Usage:
        /topic_new <name>
        /topic_new <name> <absolute_path>

    With a path, the topic is initialised in tmux mode pointing at that
    directory — i.e. the next message in this slot will spawn a Claude Code
    tmux session in that project. Without a path, the slot uses the bot's
    default cwd until the user later runs ``/topic_cwd <path>``.
    """
    if await _reject_non_private(message):
        return
    user = message.from_user
    if user is None:
        return
    raw = (command.args or "").strip().split(maxsplit=1)
    if not raw:
        await message.answer(t("ui.vt_usage_new"), parse_mode="HTML")
        return
    name = raw[0]
    cwd_arg: str | None = None
    if len(raw) > 1:
        cwd_arg = raw[1].strip()
        if not os.path.isabs(cwd_arg):
            await message.answer(
                t("ui.vt_cwd_must_be_absolute", path=_escape(cwd_arg)),
                parse_mode="HTML",
            )
            return
        if not os.path.isdir(cwd_arg):
            await message.answer(
                t("ui.vt_cwd_missing", path=_escape(cwd_arg)),
                parse_mode="HTML",
            )
            return

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

    # Seed the topic_config row even when the user didn't pass a cwd, so the
    # first message lands on a fully-populated record (tmux + claude opus
    # defaults) instead of the lazy "" defaults from `_default_topic()`. If
    # this write fails the slot still exists in the virtual-topics store —
    # the user will just see default behaviour for the slot until they run
    # /topic_cwd, which keeps the failure mode honest rather than silent.
    ok = await topic_config.initialize_topic(
        slot.thread_id,
        name=slot.name,
        cwd=cwd_arg,
        exec_mode="tmux",
        engine="claude",
        stream_mode="live",
        mode="free",
    )
    if not ok:
        await message.answer(t("ui.vt_init_failed"), parse_mode="HTML")
        return

    if cwd_arg is None:
        await message.answer(
            t("ui.vt_created", name=_escape(slot.name)),
            parse_mode="HTML",
        )
    else:
        await message.answer(
            t("ui.vt_created_with_cwd", name=_escape(slot.name), cwd=_escape(cwd_arg)),
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


@router.message(Command("topic_cwd"))
async def handle_topic_cwd(
    message: Message,
    command: CommandObject,
    virtual_topics: VirtualTopicsStore,
    topic_config: TopicConfig,
) -> None:
    """Bind the active virtual topic to a project directory.

    Usage:
        /topic_cwd <absolute_path>

    Validates the path is absolute and exists before persisting; otherwise the
    bot would silently fall back to ``Settings.default_cwd`` and drive the
    user nuts trying to figure out why their topic still spawns in `~`.
    """
    if await _reject_non_private(message):
        return
    user = message.from_user
    if user is None:
        return
    active = virtual_topics.current_slot(user.id)
    if active is None:
        await message.answer(t("ui.vt_current_none"), parse_mode="HTML")
        return
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(t("ui.vt_usage_cwd"), parse_mode="HTML")
        return
    path = raw
    if not os.path.isabs(path):
        await message.answer(
            t("ui.vt_cwd_must_be_absolute", path=_escape(path)),
            parse_mode="HTML",
        )
        return
    if not os.path.isdir(path):
        await message.answer(
            t("ui.vt_cwd_missing", path=_escape(path)),
            parse_mode="HTML",
        )
        return

    ok = await topic_config.update_cwd(active.thread_id, path)
    if not ok:
        await message.answer(t("ui.vt_init_failed"), parse_mode="HTML")
        return
    await message.answer(
        t("ui.vt_cwd_set", name=_escape(active.name), cwd=_escape(path)),
        parse_mode="HTML",
    )


@router.message(Command("topic_info"))
async def handle_topic_info(
    message: Message,
    virtual_topics: VirtualTopicsStore,
    topic_config: TopicConfig,
) -> None:
    """Show the active topic's full configuration (cwd / engine / model / modes)."""
    if await _reject_non_private(message):
        return
    user = message.from_user
    if user is None:
        return
    active = virtual_topics.current_slot(user.id)
    if active is None:
        await message.answer(t("ui.vt_current_none"), parse_mode="HTML")
        return
    settings = topic_config.get_topic(active.thread_id)
    cwd_value = settings.cwd if settings.cwd else "—"
    model_value = settings.model if settings.model else "—"
    await message.answer(
        t(
            "ui.vt_info",
            name=_escape(active.name),
            cwd=_escape(cwd_value),
            engine=_escape(engine_display_name(settings.engine)),
            model=_escape(model_value),
            exec_mode=_escape(settings.exec_mode),
            stream_mode=_escape(settings.stream_mode),
            prompt_mode=_escape(settings.mode),
        ),
        parse_mode="HTML",
    )
