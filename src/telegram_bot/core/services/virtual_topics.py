"""Virtual topics — per-user named project slots inside private chats.

Background
----------
The bot's existing topic system keys everything by `(chat_id, thread_id)`,
where `thread_id` is the Telegram forum-topic id. In a private (1-on-1) chat
`thread_id` is `None`, which forces the whole forum-only branch in commands
like `/engine`, `/mode`, `/stream`, and prevents per-project isolation.

Virtual topics solve this by giving each user a personal namespace of named
"slots". Each active slot synthesises a deterministic integer thread-id far
above the Telegram range so the rest of the codebase keeps working unchanged.

Design choices
--------------
- Synthetic ids start at `VIRTUAL_OFFSET = 10**9` — Telegram forum thread_ids
  in real supergroups stay in the low thousands, so collision is impossible.
- Slot ids are allocated from a single global counter (`next_slot_id`). This
  keeps the synthetic-thread-id flat (no per-user shard math) and avoids
  collisions across users when topic_config.json keys topics by thread_id only.
- State file (`virtual_topics.json`) lives next to `topic_config.json`. mtime-
  cached read mirrors the TopicConfig pattern; writes are atomic via
  tmp-file + os.replace under an asyncio lock.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

logger = logging.getLogger(__name__)

VIRTUAL_OFFSET: int = 10**9
"""All virtual-slot synthetic thread_ids are >= this number."""

_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,32}$")
"""Slot names: ASCII alnum + `_` + `-`, 1..32 chars. Telegram-safe and shell-safe."""


def is_virtual_thread_id(thread_id: int | None) -> bool:
    """True if this thread_id was minted by the virtual-topics store."""
    return thread_id is not None and thread_id >= VIRTUAL_OFFSET


@dataclass(frozen=True)
class VirtualSlot:
    slot_id: int
    name: str
    created_at: float

    @property
    def thread_id(self) -> int:
        return VIRTUAL_OFFSET + self.slot_id


class VirtualTopicsStore:
    """Per-user named project slots backed by `virtual_topics.json`.

    Public surface is intentionally small:
        - current_thread_id(user_id) → synthetic thread_id of active slot or None
        - list_slots(user_id) → slots in creation order
        - get_slot_by_name(user_id, name) → VirtualSlot or None
        - get_slot_by_thread_id(thread_id) → (user_id, VirtualSlot) or None
        - create_slot(user_id, name) → VirtualSlot (also becomes active)
        - switch_to(user_id, name) → VirtualSlot or None
        - delete_slot(user_id, name) → True if removed

    All mutating methods are async and serialise on `_write_lock`.
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._last_mtime: int = 0
        self._next_slot_id: int = 1
        # users[user_id] = {"active": slot_id|None, "slots": {slot_id: VirtualSlot}}
        self._users: dict[int, dict[str, Any]] = {}
        self._write_lock = asyncio.Lock()

    # ---- read path -----------------------------------------------------

    def _maybe_reload(self) -> None:
        try:
            st = os.stat(self._path)
        except FileNotFoundError:
            # First run: nothing on disk yet, in-memory defaults are correct.
            return
        except OSError:
            logger.warning("virtual_topics: cannot stat %s", self._path)
            return

        if st.st_mtime_ns == self._last_mtime:
            return

        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
        except json.JSONDecodeError:
            logger.warning("virtual_topics: invalid JSON in %s, keeping cache", self._path)
            self._last_mtime = st.st_mtime_ns
            return
        except OSError:
            logger.warning("virtual_topics: failed to read %s", self._path)
            return

        self._parse(raw)
        self._last_mtime = st.st_mtime_ns

    def _parse(self, raw: object) -> None:
        if not isinstance(raw, dict):
            logger.warning("virtual_topics: top-level not an object")
            return
        try:
            next_slot_id = int(raw.get("next_slot_id", 1))
        except (ValueError, TypeError):
            next_slot_id = 1

        users_in: dict[int, dict[str, Any]] = {}
        users_raw = raw.get("users", {})
        if isinstance(users_raw, dict):
            for user_key, user_value in users_raw.items():
                try:
                    user_id = int(user_key)
                except (ValueError, TypeError):
                    logger.warning("virtual_topics: non-numeric user key %r skipped", user_key)
                    continue
                if not isinstance(user_value, dict):
                    continue

                slots: dict[int, VirtualSlot] = {}
                raw_slots = user_value.get("slots", {})
                if isinstance(raw_slots, dict):
                    for slot_key, slot_value in raw_slots.items():
                        try:
                            slot_id = int(slot_key)
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(slot_value, dict):
                            continue
                        name = str(slot_value.get("name", "")).strip()
                        if not _NAME_RE.fullmatch(name):
                            logger.warning(
                                "virtual_topics: slot %d has invalid name %r, dropping",
                                slot_id,
                                name,
                            )
                            continue
                        try:
                            created_at = float(slot_value.get("created_at", 0.0))
                        except (ValueError, TypeError):
                            created_at = 0.0
                        slots[slot_id] = VirtualSlot(
                            slot_id=slot_id, name=name, created_at=created_at
                        )

                active_raw = user_value.get("active")
                active: int | None
                try:
                    active = int(active_raw) if active_raw is not None else None
                except (ValueError, TypeError):
                    active = None
                if active is not None and active not in slots:
                    active = None

                users_in[user_id] = {"active": active, "slots": slots}

        self._next_slot_id = max(next_slot_id, 1)
        self._users = users_in

    # ---- accessors -----------------------------------------------------

    def current_thread_id(self, user_id: int) -> int | None:
        """Return synthetic thread_id of the active slot, or None."""
        self._maybe_reload()
        user = self._users.get(user_id)
        if user is None:
            return None
        active = user.get("active")
        if active is None:
            return None
        slot = user["slots"].get(active)
        return slot.thread_id if slot is not None else None

    def current_slot(self, user_id: int) -> VirtualSlot | None:
        self._maybe_reload()
        user = self._users.get(user_id)
        if user is None:
            return None
        active = user.get("active")
        if active is None:
            return None
        return cast("VirtualSlot | None", user["slots"].get(active))

    def list_slots(self, user_id: int) -> list[VirtualSlot]:
        self._maybe_reload()
        user = self._users.get(user_id)
        if user is None:
            return []
        slots: dict[int, VirtualSlot] = user["slots"]
        return sorted(slots.values(), key=lambda s: s.created_at)

    def get_slot_by_name(self, user_id: int, name: str) -> VirtualSlot | None:
        self._maybe_reload()
        user = self._users.get(user_id)
        if user is None:
            return None
        for slot in cast("dict[int, VirtualSlot]", user["slots"]).values():
            if slot.name == name:
                return slot
        return None

    def get_slot_by_thread_id(self, thread_id: int) -> tuple[int, VirtualSlot] | None:
        """Reverse-lookup: which (user, slot) owns this synthetic thread_id?"""
        self._maybe_reload()
        if thread_id < VIRTUAL_OFFSET:
            return None
        slot_id = thread_id - VIRTUAL_OFFSET
        for user_id, user in self._users.items():
            slot = user["slots"].get(slot_id)
            if slot is not None:
                return user_id, slot
        return None

    # ---- mutation ------------------------------------------------------

    async def create_slot(self, user_id: int, name: str) -> VirtualSlot:
        """Create a new slot and make it active. Raises ValueError on bad input or duplicate."""
        if not _NAME_RE.fullmatch(name):
            raise ValueError("name must match [A-Za-z0-9_-]{1,32}")
        async with self._write_lock:
            self._maybe_reload()
            user = self._users.setdefault(user_id, {"active": None, "slots": {}})
            for existing in user["slots"].values():
                if existing.name == name:
                    raise ValueError(f"slot named {name!r} already exists")

            slot_id = self._next_slot_id
            self._next_slot_id += 1
            slot = VirtualSlot(slot_id=slot_id, name=name, created_at=time.time())
            user["slots"][slot_id] = slot
            user["active"] = slot_id
            self._save_locked()
            logger.info(
                "virtual_topics: created slot %d (%r) for user %d, set as active",
                slot_id,
                name,
                user_id,
            )
            return slot

    async def switch_to(self, user_id: int, name: str) -> VirtualSlot | None:
        """Switch active slot to the one named `name`. Returns slot or None if not found."""
        async with self._write_lock:
            self._maybe_reload()
            user = self._users.get(user_id)
            if user is None:
                return None
            target: VirtualSlot | None = None
            for slot in user["slots"].values():
                if slot.name == name:
                    target = slot
                    break
            if target is None:
                return None
            user["active"] = target.slot_id
            self._save_locked()
            logger.info(
                "virtual_topics: switched user %d to slot %d (%r)",
                user_id,
                target.slot_id,
                target.name,
            )
            return target

    async def delete_slot(self, user_id: int, name: str) -> bool:
        async with self._write_lock:
            self._maybe_reload()
            user = self._users.get(user_id)
            if user is None:
                return False
            victim_id: int | None = None
            for slot_id, slot in user["slots"].items():
                if slot.name == name:
                    victim_id = slot_id
                    break
            if victim_id is None:
                return False
            del user["slots"][victim_id]
            if user.get("active") == victim_id:
                # Pick the most recently created remaining slot, or None.
                if user["slots"]:
                    user["active"] = max(
                        user["slots"].values(),
                        key=lambda s: s.created_at,
                    ).slot_id
                else:
                    user["active"] = None
            self._save_locked()
            logger.info(
                "virtual_topics: deleted slot %d (%r) for user %d",
                victim_id,
                name,
                user_id,
            )
            return True

    async def deactivate(self, user_id: int) -> bool:
        """Clear the active slot for `user_id`. Returns True if anything changed."""
        async with self._write_lock:
            self._maybe_reload()
            user = self._users.get(user_id)
            if user is None or user.get("active") is None:
                return False
            user["active"] = None
            self._save_locked()
            logger.info("virtual_topics: deactivated user %d", user_id)
            return True

    async def ensure_default(self, user_id: int, default_name: str = "default") -> VirtualSlot:
        """Idempotently ensure the user has at least one slot and an active selection.

        Used by handlers in private chat as the lazy auto-create path so the user
        can talk to the bot without manually running `/topic_new` first.
        """
        async with self._write_lock:
            self._maybe_reload()
            user = self._users.setdefault(user_id, {"active": None, "slots": {}})
            if user["slots"]:
                if user.get("active") is None:
                    user["active"] = next(iter(user["slots"].keys()))
                    self._save_locked()
                active_slot: VirtualSlot = user["slots"][user["active"]]
                return active_slot
            slot_id = self._next_slot_id
            self._next_slot_id += 1
            slot = VirtualSlot(slot_id=slot_id, name=default_name, created_at=time.time())
            user["slots"][slot_id] = slot
            user["active"] = slot_id
            self._save_locked()
            logger.info(
                "virtual_topics: auto-created default slot %d for user %d",
                slot_id,
                user_id,
            )
            return slot

    # ---- persistence ---------------------------------------------------

    def _save_locked(self) -> None:
        """Serialize current in-memory state to disk atomically.

        Caller must hold `self._write_lock`.
        """
        data: dict[str, Any] = {
            "next_slot_id": self._next_slot_id,
            "users": {
                str(user_id): {
                    "active": user["active"],
                    "slots": {
                        str(slot.slot_id): {
                            "name": slot.name,
                            "created_at": slot.created_at,
                        }
                        for slot in user["slots"].values()
                    },
                }
                for user_id, user in self._users.items()
            },
        }
        path = Path(self._path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("virtual_topics: write failed: %s", exc)
            return
        with contextlib.suppress(OSError):
            self._last_mtime = path.stat().st_mtime_ns
