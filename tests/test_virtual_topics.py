"""Tests for the virtual-topics store and channel-key resolver."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from telegram_bot.core.services.virtual_topics import (
    VIRTUAL_OFFSET,
    VirtualTopicsStore,
    is_virtual_thread_id,
)
from telegram_bot.core.types import resolve_channel_key


def _store(tmp_path: Path) -> VirtualTopicsStore:
    return VirtualTopicsStore(str(tmp_path / "virtual_topics.json"))


def _fake_message(
    chat_id: int,
    user_id: int,
    *,
    chat_type: str = "private",
    thread_id: int | None = None,
) -> Any:
    """Construct a duck-typed object that matches the tiny aiogram surface we use."""
    chat = SimpleNamespace(id=chat_id, type=chat_type)
    user = SimpleNamespace(id=user_id)
    return SimpleNamespace(
        chat=chat,
        from_user=user,
        message_thread_id=thread_id,
    )


def test_is_virtual_thread_id_boundaries() -> None:
    assert not is_virtual_thread_id(None)
    assert not is_virtual_thread_id(0)
    assert not is_virtual_thread_id(VIRTUAL_OFFSET - 1)
    assert is_virtual_thread_id(VIRTUAL_OFFSET)
    assert is_virtual_thread_id(VIRTUAL_OFFSET + 42)


def test_create_slot_makes_it_active_and_returns_synthetic_thread_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    user_id = 196185842

    async def go() -> None:
        slot = await store.create_slot(user_id, "my-app")
        assert slot.name == "my-app"
        assert slot.thread_id == VIRTUAL_OFFSET + slot.slot_id
        assert store.current_thread_id(user_id) == slot.thread_id
        assert store.current_slot(user_id) == slot

    import asyncio

    asyncio.run(go())


def test_create_slot_rejects_duplicate_name(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def go() -> None:
        await store.create_slot(1, "my-app")
        with pytest.raises(ValueError):
            await store.create_slot(1, "my-app")

    import asyncio

    asyncio.run(go())


@pytest.mark.parametrize(
    "name",
    ["", "a" * 33, "with space", "weird/slash", "ru-имя", "tab\tname"],
)
def test_create_slot_rejects_invalid_names(tmp_path: Path, name: str) -> None:
    store = _store(tmp_path)

    async def go() -> None:
        with pytest.raises(ValueError):
            await store.create_slot(1, name)

    import asyncio

    asyncio.run(go())


def test_switch_to_changes_active_only_for_existing_slot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    user_id = 1

    async def go() -> None:
        a = await store.create_slot(user_id, "alpha")
        b = await store.create_slot(user_id, "beta")
        # `create_slot` makes the new one active — last create wins.
        assert store.current_thread_id(user_id) == b.thread_id
        switched = await store.switch_to(user_id, "alpha")
        assert switched is not None and switched.slot_id == a.slot_id
        assert store.current_thread_id(user_id) == a.thread_id
        # Non-existent slot is a no-op.
        assert await store.switch_to(user_id, "missing") is None
        assert store.current_thread_id(user_id) == a.thread_id

    import asyncio

    asyncio.run(go())


def test_delete_slot_rolls_active_to_remaining(tmp_path: Path) -> None:
    store = _store(tmp_path)
    user_id = 1

    async def go() -> None:
        a = await store.create_slot(user_id, "alpha")
        b = await store.create_slot(user_id, "beta")
        # `b` is active because it was created last.
        assert store.current_slot(user_id) == b
        ok = await store.delete_slot(user_id, "beta")
        assert ok
        # Active rolls over to the most-recently-created remaining slot, which is `a`.
        assert store.current_slot(user_id) == a
        ok = await store.delete_slot(user_id, "alpha")
        assert ok
        assert store.current_slot(user_id) is None
        assert store.current_thread_id(user_id) is None

    import asyncio

    asyncio.run(go())


def test_deactivate_clears_active_without_dropping_slots(tmp_path: Path) -> None:
    store = _store(tmp_path)
    user_id = 1

    async def go() -> None:
        await store.create_slot(user_id, "alpha")
        assert await store.deactivate(user_id) is True
        # No active selection — but the slot still exists for /topics listing.
        assert store.current_slot(user_id) is None
        assert any(s.name == "alpha" for s in store.list_slots(user_id))
        # Idempotent — second call reports nothing changed.
        assert await store.deactivate(user_id) is False

    import asyncio

    asyncio.run(go())


def test_persisted_state_survives_reload(tmp_path: Path) -> None:
    path = tmp_path / "virtual_topics.json"
    user_id = 7

    async def go() -> None:
        store = VirtualTopicsStore(str(path))
        slot = await store.create_slot(user_id, "alpha")
        await store.create_slot(user_id, "beta")
        await store.switch_to(user_id, "alpha")

        # Fresh store reads from disk on first access.
        reloaded = VirtualTopicsStore(str(path))
        active = reloaded.current_slot(user_id)
        assert active is not None and active.slot_id == slot.slot_id
        names = {s.name for s in reloaded.list_slots(user_id)}
        assert names == {"alpha", "beta"}

    import asyncio

    asyncio.run(go())


def test_persisted_state_keeps_active_null_when_deactivated(tmp_path: Path) -> None:
    path = tmp_path / "virtual_topics.json"

    async def go() -> None:
        store = VirtualTopicsStore(str(path))
        await store.create_slot(1, "alpha")
        await store.deactivate(1)

    import asyncio

    asyncio.run(go())

    raw = json.loads(path.read_text())
    assert raw["users"]["1"]["active"] is None


def test_global_slot_id_counter_avoids_collisions_across_users(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def go() -> None:
        a = await store.create_slot(1, "alpha")
        b = await store.create_slot(2, "alpha")  # same name, different user — fine
        # Slot ids are globally unique → synthetic thread_ids never collide,
        # which matters because topic_config.json is keyed by thread_id alone.
        assert a.slot_id != b.slot_id
        assert a.thread_id != b.thread_id

    import asyncio

    asyncio.run(go())


def test_resolve_channel_key_passes_through_in_forum(tmp_path: Path) -> None:
    store = _store(tmp_path)

    msg = _fake_message(chat_id=-100123, user_id=1, chat_type="supergroup", thread_id=42)
    assert resolve_channel_key(msg, store) == (-100123, 42)


def test_resolve_channel_key_returns_none_when_no_active_slot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    msg = _fake_message(chat_id=1, user_id=1, chat_type="private", thread_id=None)
    assert resolve_channel_key(msg, store) == (1, None)


def test_resolve_channel_key_substitutes_active_virtual_slot(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def setup() -> int:
        slot = await store.create_slot(123, "alpha")
        return slot.thread_id

    import asyncio

    synthetic = asyncio.run(setup())

    msg = _fake_message(chat_id=123, user_id=123, chat_type="private", thread_id=None)
    assert resolve_channel_key(msg, store) == (123, synthetic)


def test_resolve_channel_key_does_not_substitute_in_groups(tmp_path: Path) -> None:
    """A virtual-slot resolver must not steer group messages into a private slot."""
    store = _store(tmp_path)

    async def setup() -> None:
        await store.create_slot(123, "alpha")

    import asyncio

    asyncio.run(setup())

    msg = _fake_message(chat_id=-100, user_id=123, chat_type="group", thread_id=None)
    assert resolve_channel_key(msg, store) == (-100, None)
