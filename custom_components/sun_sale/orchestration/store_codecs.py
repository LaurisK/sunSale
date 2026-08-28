"""Declarative registry for the coordinator's singleton persistent stores.

The rolling *sample-history* stores are already captured declaratively in
:mod:`.history_stores` (:class:`HistoryStoreSpec`). This module gives the
remaining *singleton* stores — one value per store, not a rolling sample list —
the same treatment: each store's serialise/deserialise codec pair, plus an
optional empty-value factory, lives in one :class:`SingletonStoreSpec` table
entry instead of a hand-written function pair and a per-store setup block in
``coordinator.py``.

Deliberately excluded: the capacity store. Its deserialiser closes over runtime
config (``round_trip_efficiency``, ``nominal_capacity_kwh``) and carries the
schema-purge logic in :meth:`CapacityEstimator.from_dict`, so it stays a bespoke
declaration in the coordinator's ``async_setup``.

On-disk compatibility is load-bearing: the storage keys, ``STORAGE_VERSION``,
and every payload shape here are byte-identical to the codecs they replaced.
``tests/test_store_roundtrip.py`` pins that with golden payloads.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from ..contract.const import (
    STORAGE_KEY_ARRAY_CALIBRATION,
    STORAGE_KEY_BAKED_OBSERVED,
    STORAGE_KEY_CONSUMPTION_DAILY,
    STORAGE_KEY_COUNTER_SNAPSHOT,
    STORAGE_KEY_FORECAST_QUALITY,
    STORAGE_KEY_MODE_HISTORY,
    STORAGE_KEY_MONTHLY_BILL,
    STORAGE_KEY_PRICE_CURVE_HISTORY,
    STORAGE_KEY_PRICE_HISTORY,
    STORAGE_KEY_YESTERDAY,
)
from ..contract.models import (
    ArrayCalibration,
    BakedDayRecord,
    BakedObservedHistory,
    ConsumptionDailyBuckets,
    ConsumptionDayRecord,
    CounterSnapshotHistory,
    CounterSnapshotRecord,
    DailyPeak,
    DayClass,
    DayFeatureVintage,
    ForecastQualityStore,
    InverterModeChange,
    InverterModeHistory,
    MonthlyBillState,
    PriceCurveHistory,
    PriceDayRecord,
    PriceEntry,
    SlotKwh,
    SolarEntry,
    StorageMode,
)
from ..pipeline import forecast_accuracy

# ---------------------------------------------------------------------------
# Spec type + registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SingletonStoreSpec:
    """Declarative spec for one non-history persistent store.

    Attributes:
        storage_key: Domain-global base storage key; the coordinator applies
            :func:`entry_storage_key` to namespace it per config entry.
        serialize: Convert the stored value to a JSON-serialisable dict.
        deserialize: Reconstruct the stored value from a payload dict.
        default: Factory for the empty value used when the store holds nothing
            yet (fresh install / first cycle). ``None`` when the read sites
            treat an absent value as meaningful (e.g. the monthly-bill ledger).
    """

    storage_key: str
    serialize: Callable[[Any], dict]
    deserialize: Callable[[dict], Any]
    default: Callable[[], Any] | None = field(default=None)


# ---------------------------------------------------------------------------
# Yesterday/today two-bucket state
# ---------------------------------------------------------------------------

@dataclass
class YesterdayBuckets:
    """In-memory state for the two-bucket yesterday/today price + solar store."""

    yesterday_date: str | None = None
    yesterday_nordpool: list[PriceEntry] = field(default_factory=list)
    yesterday_solar: list[SolarEntry] = field(default_factory=list)
    today_date: str | None = None
    today_nordpool: list[PriceEntry] = field(default_factory=list)
    today_solar: list[SolarEntry] = field(default_factory=list)


def _parse_nordpool_entries(payload: dict) -> list[PriceEntry]:
    """Deserialise a list of price entries from a stored payload dict."""
    return [
        PriceEntry(
            start=datetime.fromisoformat(e["start"]),
            end=datetime.fromisoformat(e["end"]),
            price_eur_kwh=e["price"],
        )
        for e in payload.get("nordpool", [])
    ]


def _parse_solar_entries(payload: dict) -> list[SolarEntry]:
    """Deserialise a list of solar entries from a stored payload dict."""
    return [
        SolarEntry(
            start=datetime.fromisoformat(e["start"]),
            end=datetime.fromisoformat(e["end"]),
            expected_kwh=e["kwh"],
            source=e["source"],
        )
        for e in payload.get("solar", [])
    ]


def _serialize_yesterday(buckets: YesterdayBuckets) -> dict:
    """Serialise yesterday buckets to the two-bucket storage layout."""
    def _ser_nordpool(xs: list[PriceEntry]) -> list[dict]:
        """Serialise a list of Nordpool price entries to dicts."""
        return [{"start": e.start.isoformat(), "end": e.end.isoformat(), "price": e.price_eur_kwh} for e in xs]

    def _ser_solar(xs: list[SolarEntry]) -> list[dict]:
        """Serialise a list of solar forecast entries to dicts."""
        return [
            {"start": e.start.isoformat(), "end": e.end.isoformat(), "kwh": e.expected_kwh, "source": e.source}
            for e in xs
        ]

    return {
        "yesterday": {
            "date":     buckets.yesterday_date,
            "nordpool": _ser_nordpool(buckets.yesterday_nordpool),
            "solar":    _ser_solar(buckets.yesterday_solar),
        },
        "today": {
            "date":     buckets.today_date,
            "nordpool": _ser_nordpool(buckets.today_nordpool),
            "solar":    _ser_solar(buckets.today_solar),
        },
    }


def _deserialize_yesterday(d: dict) -> YesterdayBuckets:
    """Deserialise yesterday buckets, handling the legacy single-bucket layout."""
    if "yesterday" in d or "today" in d:
        y = d.get("yesterday") or {}
        t = d.get("today") or {}
        return YesterdayBuckets(
            yesterday_date=y.get("date"),
            yesterday_nordpool=_parse_nordpool_entries(y),
            yesterday_solar=_parse_solar_entries(y),
            today_date=t.get("date"),
            today_nordpool=_parse_nordpool_entries(t),
            today_solar=_parse_solar_entries(t),
        )
    # Legacy single-bucket layout {"date","nordpool","solar"} — treat as yesterday.
    return YesterdayBuckets(
        yesterday_date=d.get("date"),
        yesterday_nordpool=_parse_nordpool_entries(d),
        yesterday_solar=_parse_solar_entries(d),
    )


def rotate_yesterday_buckets(
    buckets: YesterdayBuckets,
    today_str: str,
    today_nordpool: list[PriceEntry],
    today_solar: list[SolarEntry],
) -> YesterdayBuckets:
    """Apply day-rollover rotation and replace the today slice.

    On the first save of a new local day the previous today bucket becomes
    yesterday; within the same day only the today slice is overwritten.

    Args:
        buckets: Current bucket state.
        today_str: ISO date string for the current local day.
        today_nordpool: Today's price entries to store.
        today_solar: Today's solar entries to store.

    Returns:
        Updated YesterdayBuckets.
    """
    if buckets.today_date is not None and buckets.today_date != today_str:
        return YesterdayBuckets(
            yesterday_date=buckets.today_date,
            yesterday_nordpool=buckets.today_nordpool,
            yesterday_solar=buckets.today_solar,
            today_date=today_str,
            today_nordpool=today_nordpool,
            today_solar=today_solar,
        )
    return YesterdayBuckets(
        yesterday_date=buckets.yesterday_date,
        yesterday_nordpool=buckets.yesterday_nordpool,
        yesterday_solar=buckets.yesterday_solar,
        today_date=today_str,
        today_nordpool=today_nordpool,
        today_solar=today_solar,
    )


# ---------------------------------------------------------------------------
# Per-store serialisation codecs
# ---------------------------------------------------------------------------

def _serialize_consumption_daily(buckets: ConsumptionDailyBuckets) -> dict:
    """Serialise the rolling per-day hour-bucket consumption history."""
    return {
        "records": [
            {
                "date":  r.local_date.isoformat(),
                "kwh":   list(r.hour_kwh),
                "cov":   list(r.hour_completeness),
                "at":    r.finalised_at.isoformat(),
            }
            for r in buckets.records
        ]
    }


def _deserialize_consumption_daily(d: dict) -> ConsumptionDailyBuckets:
    """Deserialise the rolling per-day hour-bucket consumption history.

    Malformed records are skipped silently so a single bad row cannot block
    startup. Records that don't carry a full 24-tuple for either ``kwh`` or
    ``cov`` are dropped — the builder requires fixed shape.
    """
    records: list[ConsumptionDayRecord] = []
    for r in d.get("records", []):
        try:
            kwh = tuple(float(x) for x in r["kwh"])
            cov = tuple(float(x) for x in r["cov"])
            if len(kwh) != 24 or len(cov) != 24:
                continue
            records.append(
                ConsumptionDayRecord(
                    local_date=date.fromisoformat(r["date"]),
                    hour_kwh=kwh,
                    hour_completeness=cov,
                    finalised_at=datetime.fromisoformat(r["at"]),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return ConsumptionDailyBuckets(records=tuple(records))


def _serialize_price_history(peaks: list[DailyPeak]) -> dict:
    """Serialise a list of daily peaks."""
    return {
        "peaks": [{"day": p.day.isoformat(), "peak": p.peak_eur_kwh, "class": p.day_class.value} for p in peaks]
    }


def _deserialize_price_history(d: dict) -> list[DailyPeak]:
    """Deserialise a list of daily peaks, silently skipping malformed entries."""
    result: list[DailyPeak] = []
    for p in d.get("peaks", []):
        try:
            result.append(DailyPeak(
                day=date.fromisoformat(p["day"]),
                peak_eur_kwh=p["peak"],
                day_class=DayClass(p["class"]),
            ))
        except (KeyError, ValueError):
            continue
    return result


def _serialize_price_curve_history(history: PriceCurveHistory) -> dict:
    """Serialise the rolling settled-day price statistics.

    Keys are abbreviated because this store holds one record per day for well
    over a year; the payload is written on every rollover and the shorthand
    roughly halves it.
    """
    return {
        "records": [
            {
                "day": r.day.isoformat(),
                "cls": r.day_class.value,
                "p1": r.peak_1h_eur_kwh,
                "p3": r.peak_3h_eur_kwh,
                "t1": r.trough_1h_eur_kwh,
                "t3": r.trough_3h_eur_kwh,
                "nh": r.negative_hours,
                "mean": r.mean_eur_kwh,
                "wind": r.wind_speed_kmh,
                "temp": r.temperature_c,
                "solar": r.solar_kwh,
                "ngen": r.negative_generation_kwh,
            }
            for r in history.records
        ],
        "vintages": [
            {
                "day": v.day.isoformat(),
                "lead": v.lead_days,
                "wind": v.wind_speed_kmh,
                "temp": v.temperature_c,
                "solar": v.solar_kwh,
            }
            for v in history.vintages
        ],
    }


def _deserialize_price_curve_history(d: dict) -> PriceCurveHistory:
    """Deserialise settled-day price statistics, skipping malformed records."""
    records: list[PriceDayRecord] = []
    for r in d.get("records", []):
        try:
            records.append(PriceDayRecord(
                day=date.fromisoformat(r["day"]),
                day_class=DayClass(r["cls"]),
                peak_1h_eur_kwh=float(r["p1"]),
                peak_3h_eur_kwh=float(r["p3"]),
                trough_1h_eur_kwh=float(r["t1"]),
                trough_3h_eur_kwh=float(r["t3"]),
                negative_hours=float(r["nh"]),
                mean_eur_kwh=float(r["mean"]),
                wind_speed_kmh=_opt_float(r.get("wind")),
                temperature_c=_opt_float(r.get("temp")),
                solar_kwh=_opt_float(r.get("solar")),
                negative_generation_kwh=_opt_float(r.get("ngen")),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    records.sort(key=lambda r: r.day)

    vintages: list[DayFeatureVintage] = []
    for v in d.get("vintages", []):
        try:
            vintages.append(DayFeatureVintage(
                day=date.fromisoformat(v["day"]),
                lead_days=int(v.get("lead", 0)),
                wind_speed_kmh=_opt_float(v.get("wind")),
                temperature_c=_opt_float(v.get("temp")),
                solar_kwh=_opt_float(v.get("solar")),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    vintages.sort(key=lambda v: v.day)
    return PriceCurveHistory(records=tuple(records), vintages=tuple(vintages))


def _opt_float(value: Any) -> float | None:
    """Return value as a float, preserving None for absent optional fields."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _serialize_counter_snapshot(history: CounterSnapshotHistory) -> dict:
    """Serialise the rolling pre-rollover counter snapshot history."""
    return {
        "records": [
            {
                "side": r.side_id,
                "ts":   r.captured_at.isoformat(),
                "kwh":  r.today_total_kwh,
            }
            for r in history.records
        ]
    }


def _deserialize_counter_snapshot(d: dict) -> CounterSnapshotHistory:
    """Deserialise the rolling pre-rollover counter snapshot history."""
    records: list[CounterSnapshotRecord] = []
    for r in d.get("records", []):
        try:
            records.append(
                CounterSnapshotRecord(
                    side_id=r["side"],
                    captured_at=datetime.fromisoformat(r["ts"]),
                    today_total_kwh=float(r["kwh"]),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return CounterSnapshotHistory(records=tuple(records))


def _serialize_baked_observed(history: BakedObservedHistory) -> dict:
    """Serialise the rolling baked-observed history (one record per (date, side))."""
    return {
        "records": [
            {
                "date":   r.date_str,
                "side":   r.side_id,
                "ctotal": r.counter_total_used,
                "src":    r.source_kind,
                "slots":  [
                    {"s": s.start.isoformat(), "e": s.end.isoformat(), "kwh": s.kwh}
                    for s in r.baked_slots
                ],
                "sum":    r.baked_sum,
                "at":     r.baked_at.isoformat(),
            }
            for r in history.records
        ]
    }


def _deserialize_baked_observed(d: dict) -> BakedObservedHistory:
    """Deserialise the rolling baked-observed history.

    Malformed entries are skipped silently so a single bad row cannot block
    startup. Slot lists with non-parseable timestamps are dropped wholesale.
    """
    records: list[BakedDayRecord] = []
    for r in d.get("records", []):
        try:
            slots = tuple(
                SlotKwh(
                    start=datetime.fromisoformat(s["s"]),
                    end=datetime.fromisoformat(s["e"]),
                    kwh=float(s["kwh"]),
                )
                for s in r["slots"]
            )
            records.append(
                BakedDayRecord(
                    date_str=r["date"],
                    side_id=r["side"],
                    counter_total_used=float(r["ctotal"]),
                    source_kind=r["src"],
                    baked_slots=slots,
                    baked_sum=float(r["sum"]),
                    baked_at=datetime.fromisoformat(r["at"]),
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    return BakedObservedHistory(records=tuple(records))


def _serialize_mode_history(history: InverterModeHistory) -> dict:
    """Serialise the rolling inverter-mode-change history."""
    return {
        "samples": [
            {"ts": s.timestamp.isoformat(), "mode": s.mode.value, "raw_state": s.raw_state}
            for s in history.samples
        ]
    }


def _deserialize_mode_history(d: dict) -> InverterModeHistory:
    """Deserialise the rolling inverter-mode-change history.

    Unknown mode strings (e.g. from older versions) are coerced to UNKNOWN so
    the integration starts cleanly after a release that retired a mode value.
    """
    samples: list[InverterModeChange] = []
    for s in d.get("samples", []):
        try:
            mode = StorageMode(s["mode"])
        except ValueError:
            mode = StorageMode.UNKNOWN
        samples.append(
            InverterModeChange(
                timestamp=datetime.fromisoformat(s["ts"]),
                mode=mode,
                # Accept the legacy "reg" key so histories persisted before the
                # raw_state rename still load.
                raw_state=int(s["raw_state"] if "raw_state" in s else s["reg"]),
            )
        )
    return InverterModeHistory(samples=tuple(samples))


def _serialize_array_calibration(cal: ArrayCalibration) -> dict:
    """Serialise a fitted array calibration to a JSON-safe dict.

    Args:
        cal: Calibration to persist.

    Returns:
        Dict suitable for HA's Store.async_save().
    """
    return {
        "kwp_eff": round(cal.kwp_eff, 4),
        "tilt_deg": round(cal.tilt_deg, 2),
        "azimuth_deg": round(cal.azimuth_deg, 2),
        "fit_quality": round(cal.fit_quality, 4),
        "n_days": cal.n_days,
        "n_slots": cal.n_slots,
        "implied_forecast_kwp": (
            round(cal.implied_forecast_kwp, 4)
            if cal.implied_forecast_kwp is not None else None
        ),
        "correction_factor": (
            round(cal.correction_factor, 4)
            if cal.correction_factor is not None else None
        ),
        "confidence": round(cal.confidence, 4),
        "computed_at": cal.computed_at.isoformat() if cal.computed_at else None,
    }


def _deserialize_array_calibration(d: dict) -> ArrayCalibration | None:
    """Rebuild a fitted array calibration from its stored dict.

    Args:
        d: Dict from HA's Store.async_load().

    Returns:
        The restored calibration, or ``None`` when the payload is unusable —
        a partially-written calibration must not silently become a
        zero-capacity one, which would make every clear-sky index infinite.
    """
    if not d or d.get("kwp_eff") in (None, 0):
        return None
    raw_at = d.get("computed_at")
    return ArrayCalibration(
        kwp_eff=float(d["kwp_eff"]),
        tilt_deg=float(d.get("tilt_deg", 0.0)),
        azimuth_deg=float(d.get("azimuth_deg", 180.0)),
        fit_quality=float(d.get("fit_quality", 0.0)),
        n_days=int(d.get("n_days", 0)),
        n_slots=int(d.get("n_slots", 0)),
        implied_forecast_kwp=(
            float(d["implied_forecast_kwp"])
            if d.get("implied_forecast_kwp") is not None else None
        ),
        correction_factor=(
            float(d["correction_factor"])
            if d.get("correction_factor") is not None else None
        ),
        confidence=float(d.get("confidence", 0.0)),
        computed_at=datetime.fromisoformat(raw_at) if raw_at else None,
    )


def _serialize_monthly_bill(state: MonthlyBillState) -> dict:
    """Serialise the monthly bill ledger as a ``date_str → cost`` map."""
    return {"finalized_days": dict(state.finalized_days)}


def _deserialize_monthly_bill(d: dict) -> MonthlyBillState:
    """Deserialise the monthly bill ledger.

    Legacy entries (pre-ledger, carrying ``carry_eur`` / ``yday_str``) have no
    ``finalized_days`` key and deserialise to an empty ledger — the carry then
    rebuilds from the next priceable day forward (older days' prices are gone,
    so they are unrecoverable regardless).
    """
    raw = d.get("finalized_days") or {}
    return MonthlyBillState(
        finalized_days=tuple(
            (str(ds), float(cost)) for ds, cost in raw.items()
        ),
    )


# ---------------------------------------------------------------------------
# Spec registry
# ---------------------------------------------------------------------------
# Order is cosmetic — each store is independent — but kept matching the
# historical hand-written setup sequence in ``coordinator.async_setup`` for
# reviewability. The capacity store is intentionally absent (see module
# docstring).

SINGLETON_STORE_SPECS: tuple[SingletonStoreSpec, ...] = (
    SingletonStoreSpec(
        storage_key=STORAGE_KEY_CONSUMPTION_DAILY,
        serialize=_serialize_consumption_daily,
        deserialize=_deserialize_consumption_daily,
        default=lambda: ConsumptionDailyBuckets(records=()),
    ),
    SingletonStoreSpec(
        storage_key=STORAGE_KEY_PRICE_HISTORY,
        serialize=_serialize_price_history,
        deserialize=_deserialize_price_history,
    ),
    SingletonStoreSpec(
        storage_key=STORAGE_KEY_PRICE_CURVE_HISTORY,
        serialize=_serialize_price_curve_history,
        deserialize=_deserialize_price_curve_history,
        default=lambda: PriceCurveHistory(records=()),
    ),
    SingletonStoreSpec(
        storage_key=STORAGE_KEY_ARRAY_CALIBRATION,
        serialize=_serialize_array_calibration,
        deserialize=_deserialize_array_calibration,
    ),
    SingletonStoreSpec(
        storage_key=STORAGE_KEY_FORECAST_QUALITY,
        serialize=forecast_accuracy.store_to_dict,
        deserialize=forecast_accuracy.store_from_dict,
        default=ForecastQualityStore,
    ),
    SingletonStoreSpec(
        storage_key=STORAGE_KEY_MONTHLY_BILL,
        serialize=_serialize_monthly_bill,
        deserialize=_deserialize_monthly_bill,
    ),
    SingletonStoreSpec(
        storage_key=STORAGE_KEY_YESTERDAY,
        serialize=_serialize_yesterday,
        deserialize=_deserialize_yesterday,
        default=YesterdayBuckets,
    ),
    SingletonStoreSpec(
        storage_key=STORAGE_KEY_MODE_HISTORY,
        serialize=_serialize_mode_history,
        deserialize=_deserialize_mode_history,
        default=lambda: InverterModeHistory(samples=()),
    ),
    SingletonStoreSpec(
        storage_key=STORAGE_KEY_COUNTER_SNAPSHOT,
        serialize=_serialize_counter_snapshot,
        deserialize=_deserialize_counter_snapshot,
        default=lambda: CounterSnapshotHistory(records=()),
    ),
    SingletonStoreSpec(
        storage_key=STORAGE_KEY_BAKED_OBSERVED,
        serialize=_serialize_baked_observed,
        deserialize=_deserialize_baked_observed,
        default=lambda: BakedObservedHistory(records=()),
    ),
)
