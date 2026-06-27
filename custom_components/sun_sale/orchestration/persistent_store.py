"""Generic typed persistent store wrapping HA's Store helper.

Also owns per-config-entry key namespacing (:func:`entry_storage_key`) so
multiple config entries keep independent persisted state instead of clobbering
one shared file.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Generic, TypeVar

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..contract.const import DOMAIN, STORE_SAVE_DELAY_SECONDS

T = TypeVar("T")
JSONDict = dict[str, Any]


def entry_storage_key(entry_id: str, base_key: str) -> str:
    """Namespace a domain-global storage key to a config entry.

    ``sun_sale_capacity`` → ``sun_sale_<entry_id>_capacity``. Idempotent only in
    the sense that it always inserts the entry id once after the domain prefix;
    callers must pass the *base* (global) key, never an already-namespaced one.

    Args:
        entry_id: Config entry id to scope the store to.
        base_key: The domain-global base key (``f"{DOMAIN}_..."``).

    Returns:
        The per-entry storage key.
    """
    return base_key.replace(f"{DOMAIN}_", f"{DOMAIN}_{entry_id}_", 1)


class PersistentStore(Generic[T]):
    """Typed wrapper around HA's Store with optional list append-and-trim semantics.

    Folds the raw HA Store, in-memory cache, and serialisation logic into one
    object so callers need only declare a single field per persisted value.

    Args:
        hass: Home Assistant instance.
        version: Storage version integer passed to HA's Store.
        key: Storage key string passed to HA's Store.
        serialize: Convert T to a JSON-serialisable dict.
        deserialize: Reconstruct T from the stored dict; may return None to
            signal that the stored data should be treated as missing.
        save_delay: Debounce window (seconds) applied to :meth:`save`. When
            positive (the default), writes go through HA's
            ``Store.async_delay_save`` so the per-tick fan-out and off-cycle
            bursts coalesce into one background write per store. Pass ``0`` to
            force an immediate, awaited write for any store that needs
            synchronous durability.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        version: int,
        key: str,
        serialize: Callable[[T], JSONDict],
        deserialize: Callable[[JSONDict], T | None],
        save_delay: float = STORE_SAVE_DELAY_SECONDS,
    ) -> None:
        """Initialise store with serialisation callbacks; does not perform I/O."""
        self._store: Store = Store(hass, version, key)
        self._serialize = serialize
        self._deserialize = deserialize
        self._save_delay = save_delay
        self._value: T | None = None

    @property
    def value(self) -> T | None:
        """Return cached in-memory value (None until first load or save)."""
        return self._value

    async def load(self) -> T | None:
        """Load from disk, deserialise, cache, and return.

        Returns:
            Deserialised value, or None if the store is empty.
        """
        raw = await self._store.async_load()
        if raw is None:
            return None
        self._value = self._deserialize(raw)
        return self._value

    async def save(self, value: T) -> None:
        """Update the in-memory cache and persist the value.

        The cache is updated synchronously so callers (and the DAG, which reads
        :attr:`value`) always see the latest value immediately. The disk write
        is debounced via ``Store.async_delay_save`` when ``save_delay`` is
        positive — coalescing the many saves a single coordinator tick and its
        off-cycle refresh bursts trigger into one background write — or issued
        immediately when ``save_delay`` is ``0``. Serialisation is deferred to
        write time so a value superseded within the debounce window is never
        serialised.

        Args:
            value: Value to persist.
        """
        self._value = value
        if self._save_delay > 0:
            self._store.async_delay_save(lambda: self._serialize(value), self._save_delay)
        else:
            await self._store.async_save(self._serialize(value))

    async def append_and_trim(
        self,
        new_item: Any,
        cutoff: datetime,
        timestamp_fn: Callable[[Any], datetime],
    ) -> None:
        """Append an item, discard entries older than cutoff, then save.

        Only valid when T is list[Item].

        Args:
            new_item: Item to append to the list.
            cutoff: Entries with a timestamp strictly before this are removed.
            timestamp_fn: Extracts the datetime key from each list element.
        """
        items: list = list(self._value or [])
        items = [x for x in items if timestamp_fn(x) >= cutoff]
        items.append(new_item)
        await self.save(items)  # type: ignore[arg-type]
