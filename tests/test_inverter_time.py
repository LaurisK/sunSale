"""Tests for inbound/inverter_time.py — pure Python."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from custom_components.sun_sale.contract.models import InverterTimeReading
from custom_components.sun_sale.inbound.inverter_time import (
    InverterTimeHistory,
    InverterTimeTranslator,
    current_skew_seconds,
    empty_history,
    update_history,
)


LOCAL_TZ = timezone.utc


@dataclass(frozen=True)
class _State:
    state: object
    attributes: dict = None  # type: ignore[assignment]


class _Hass:
    """Minimal hass stub exposing ``states.get``."""

    def __init__(self, states: dict[str, _State] | None = None) -> None:
        self.states = self  # type: ignore[assignment]
        self._states = states or {}

    def get(self, entity_id: str):
        return self._states.get(entity_id)


def _reading(ha_offset_s: float, inv_offset_s: float) -> InverterTimeReading:
    """Build a reading with HA = base+ha_offset and inverter = base+inv_offset."""
    base = datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)
    return InverterTimeReading(
        ha_now=base + timedelta(seconds=ha_offset_s),
        inverter_now=base + timedelta(seconds=inv_offset_s),
    )


# ---------------------------------------------------------------------------
# History updates + skew calculation
# ---------------------------------------------------------------------------


def test_empty_history_returns_none_skew() -> None:
    """Skew is undefined while the rolling buffer is empty."""
    assert current_skew_seconds(empty_history()) is None


def test_below_min_samples_returns_none_skew() -> None:
    """Skew is suppressed until min_samples reached (default 5)."""
    h = empty_history()
    for i in range(4):
        h = update_history(h, _reading(i, i + 60))  # constant +60s skew
    assert current_skew_seconds(h) is None


def test_at_min_samples_returns_median_skew() -> None:
    """Once min_samples is reached the median is exposed."""
    h = empty_history()
    for i in range(5):
        h = update_history(h, _reading(i, i + 60))
    skew = current_skew_seconds(h)
    assert skew == 60


def test_negative_skew_inverter_behind_ha() -> None:
    """Negative values represent the inverter being behind HA."""
    h = empty_history()
    for i in range(5):
        h = update_history(h, _reading(i, i - 120))
    assert current_skew_seconds(h) == -120


def test_median_rejects_outliers() -> None:
    """A single bogus reading does not dominate the median."""
    h = empty_history()
    for i in range(4):
        h = update_history(h, _reading(i, i + 30))     # 4×+30s
    h = update_history(h, _reading(4, 4 + 10000))      # one outlier
    assert current_skew_seconds(h) == 30


def test_update_trims_to_max_samples() -> None:
    """History never grows past max_samples; oldest samples are dropped."""
    h = empty_history()
    for i in range(60):
        h = update_history(h, _reading(i, i + 30), max_samples=50)
    assert len(h.samples) == 50
    # The oldest sample retained should be at i=10 (60-50).
    assert h.samples[0].ha_now == _reading(10, 40).ha_now


def test_update_none_reading_no_op() -> None:
    """Passing ``None`` returns the same history instance unchanged."""
    h = update_history(empty_history(), _reading(0, 60))
    out = update_history(h, None)
    assert out is h


# ---------------------------------------------------------------------------
# Translator parsing
# ---------------------------------------------------------------------------


def test_translator_parses_iso_local_time() -> None:
    """A naive ISO timestamp is interpreted in the configured local timezone."""
    ha_now = datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)
    # Inverter reports 12:01 local (= 12:01 UTC because LOCAL_TZ is UTC).
    state = _State(state="2024-01-15T12:01:00")
    hass = _Hass({"sensor.inv_clock": state})
    t = InverterTimeTranslator("sensor.inv_clock", local_tz=LOCAL_TZ)

    reading = t.parse(hass, now=ha_now)
    assert reading is not None
    assert reading.ha_now == ha_now
    # Inverter ahead of HA by 60s.
    assert (reading.inverter_now - reading.ha_now).total_seconds() == 60


def test_translator_handles_tz_aware_state() -> None:
    """An ISO timestamp carrying tz info is preserved verbatim."""
    ha_now = datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)
    state = _State(state="2024-01-15T12:00:30+00:00")
    hass = _Hass({"sensor.inv_clock": state})
    t = InverterTimeTranslator("sensor.inv_clock", local_tz=LOCAL_TZ)

    reading = t.parse(hass, now=ha_now)
    assert reading is not None
    assert (reading.inverter_now - reading.ha_now).total_seconds() == 30


def test_translator_returns_none_when_unavailable() -> None:
    """Common HA unavailable states map to ``None``."""
    hass = _Hass({"sensor.inv_clock": _State(state="unavailable")})
    t = InverterTimeTranslator("sensor.inv_clock", local_tz=LOCAL_TZ)
    assert t.parse(hass, now=datetime.now(timezone.utc)) is None


def test_translator_returns_none_when_unparseable() -> None:
    """A garbage state string yields ``None`` rather than crashing."""
    hass = _Hass({"sensor.inv_clock": _State(state="not a datetime")})
    t = InverterTimeTranslator("sensor.inv_clock", local_tz=LOCAL_TZ)
    assert t.parse(hass, now=datetime.now(timezone.utc)) is None


def test_translator_empty_entity_id_returns_none() -> None:
    """An unconfigured entity ID disables the translator."""
    t = InverterTimeTranslator("", local_tz=LOCAL_TZ)
    assert t.parse(_Hass(), now=datetime.now(timezone.utc)) is None


def test_translator_accepts_datetime_state() -> None:
    """A ``datetime`` state (as HA's datetime platform may surface) is used directly."""
    ha_now = datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc)
    inv_local = datetime(2024, 1, 15, 11, 59, 30)   # naive local
    state = _State(state=inv_local)
    hass = _Hass({"sensor.inv_clock": state})
    t = InverterTimeTranslator("sensor.inv_clock", local_tz=LOCAL_TZ)

    reading = t.parse(hass, now=ha_now)
    assert reading is not None
    # Inverter behind HA by 30s.
    assert (reading.inverter_now - reading.ha_now).total_seconds() == -30
