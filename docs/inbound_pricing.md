# Inbound — Pricing module

Reference for `custom_components/sun_sale/inbound/pricing.py`.

The pricing module owns the **72h yesterday→today→tomorrow `PriceSeries`** that every downstream consumer (calculation, schedule, dashboard, sensors) reads. It is pure Python with no Home Assistant imports.

## Summary

A price-feed translator reads the configured market sensor and emits the source-agnostic `PriceFeedData` (today + tomorrow). `build_price_series_72h(feed, yesterday, config, now)` combines `YesterdayPrices.entries + feed.entries`, applies the tariff formula, and yields the 72h `PriceSeries` `PricingNode` returns.

This module is the **price-source seam** (multi-region): `build_price_translator` selects one of `NordpoolTranslator` (Nordics), `EntsoeTranslator` (EU), `OctopusAgileTranslator` (UK), `AmberTranslator` (AU), or `TouScheduleTranslator` (synthetic TOU), all emitting the identical `PriceFeedData` shape (`NordpoolData` is a back-compat **alias** of `PriceFeedData`). See `CLAUDE.md` → *Price sources & sell-price models*. The Nordpool path is the default and is documented in detail below; the other translators share the same assembly helpers (`_build_feed` / `_parse_points`).

- **Exposes:** `PriceFeedData` (translators), `PriceSeries` (helper), `build_price_translator`.
- **Depends on:** `contract.models`, `pipeline.tariff`.
- **Tests:** `tests/test_pricing.py` (helper) + `tests/test_coordinator.py::test_nordpool_*` and the per-source translator tests. Full per-test coverage table in §8.

## Contents

1. [Responsibilities](#1-responsibilities)
2. [Public API](#2-public-api)
3. [Inputs](#3-inputs)
4. [Output: `PriceSeries`](#4-output-priceseries)
5. [Tariff formula](#5-tariff-formula)
6. [Resolution handling](#6-resolution-handling)
7. [Integration in the DAG](#7-integration-in-the-dag)
8. [Tests](#8-tests)

---

## 1. Responsibilities

- Combine **yesterday entries** (from persistent store, supplied by orchestration) with **today + tomorrow entries** (from the active price-feed translator) into a single contiguous slot list.
- Apply the buy/sell tariff formula to every slot.
- Produce a `PriceSeries` keyed by the slot resolution detected by the translator (15-min or 1h).
- Provide a thin per-list helper (`build_price_series`) used by tests and any caller that already has a pre-stitched entry list.

Yesterday-stitching used to live in the coordinator as an in-place mutation of the feed entries. It is now an explicit, testable function call in this module; the coordinator only supplies the persisted entries as a primary input.

---

## 2. Public API

```python
def build_price_series(
    prices: list[PriceEntry],
    config: TariffConfig,
    now: datetime | None = None,
    resolution: timedelta | None = None,
    local_tz: tzinfo | None = None,
    source: str = "nordpool",
) -> PriceSeries
```

Apply tariff formulas to a flat list of `PriceEntry`. If `resolution` is omitted it is derived from the first two slots (defaults to 1h for single-slot input). `now` defaults to `datetime.now(timezone.utc)` and is recorded as `PriceSeries.computed_at`. `local_tz` projects each slot start to a local time-of-day for time-of-use distribution-band selection; when `None` the flat distribution fees apply to every slot. `source` is the price-source identifier recorded in each slot's provenance tag (`sources = (source, "tariff")`).

```python
def build_price_series_72h(
    feed: PriceFeedData,
    yesterday: YesterdayPrices,
    config: TariffConfig,
    now: datetime | None = None,
    local_tz: tzinfo | None = None,
    source: str = "nordpool",
) -> PriceSeries
```

Assemble the full yesterday→today→tomorrow series. Concatenates `yesterday.entries + feed.entries`, then delegates to `build_price_series` with `resolution=feed.resolution`, `local_tz`, and `source` forwarded. This is the function the DAG's `PricingNode` calls (passing `ctx.config.local_tz` and `ctx.config.price_source`).

---

## 3. Inputs

**`PriceFeedData`** (`contract/models.py`) — produced by the active price-feed translator (`NordpoolTranslator` by default; `NordpoolData` is an alias). Covers today + tomorrow only. `resolution` is auto-detected from the source attribute timestamps (for Nordpool, `raw_today[1].start - raw_today[0].start`); the sensor exposes a single resolution stream per cycle and tomorrow is zero-filled until the day-ahead market publishes. Translators may also carry a separate `export_price_eur_kwh` per entry (dual-feed sources) for the `feed` sell mode.

**`YesterdayPrices`** (`contract/models.py`) — a frozen wrapper around `tuple[PriceEntry, ...]`. Supplied by `orchestration.coordinator` from the `STORAGE_KEY_YESTERDAY` `Store`. The coordinator gates the value: if the stored date is not exactly yesterday, an empty tuple is passed (no stale stitching).

**`TariffConfig`** — user-configured fees, taxes, and markups, built once at coordinator setup from the config entry.

---

## 4. Output: `PriceSeries`

```python
@dataclass(frozen=True)
class PriceSeries:
    slots: tuple[PriceSlot, ...]
    resolution: timedelta
    computed_at: datetime

    def slot_at(self, t: datetime) -> PriceSlot | None
    def window(self, t1: datetime, t2: datetime) -> tuple[PriceSlot, ...]
```

Each `PriceSlot` carries:

| Field | Meaning |
|---|---|
| `start`, `end` | UTC slot boundaries |
| `buy_eur_kwh` | Effective grid-import price (always ≥ 0 in practice) |
| `sell_eur_kwh` | Effective grid-export revenue — **can be negative** |
| `spot_eur_kwh` | Raw import/spot price, retained for provenance |
| `export_eur_kwh` | Raw separate export-feed price (dual-feed sources), else `None` |
| `sources` | `(price_source, "tariff")` for diagnostics — e.g. `("nordpool", "tariff")` |

The pricing module emits raw buy/sell price data only. Whether a slot is sellable (the strict `> 0` check) is not a pricing concern — it lives downstream in `pipeline/slot_physics.py` / `pipeline/schedule.py`.

---

## 5. Tariff formula

Delegated to `pipeline/tariff.py`:

```
buy  = (spot + distribution_fee + markup) * (1 + tax_rate)
sell = (spot - sell_distribution_fee - sell_markup) * (1 - sell_tax_rate)
```

The `sell` formula above is the default `spot` sell mode; `TariffConfig.sell_mode` also supports `fixed` (feed-in tariff), `schedule` (TOU avoided-cost), and `feed` (separate live export price) — see `CLAUDE.md` → *Sell-price models*. `buy` is always this shape.

`sell_eur_kwh` can be negative (high sell fees against low spot prices) or exactly zero. Downstream consumers apply the strict `> 0` sellability check (in `slot_physics` / `schedule`); the pricing module itself does not flag this.

### Time-of-use distribution bands

Only the **distribution fees** are time-of-use; tax rates and markups are global. `TariffConfig` carries two band schedules — `weekday_bands` (Mon–Fri) and `weekend_bands` (Sat/Sun) — each a `tuple[TariffBand]`. A `TariffBand` has a `start_minute` (minutes from local midnight) plus its own buy/sell distribution fee. For each slot, `build_price_series` projects the slot start into `local_tz`, picks the weekday/weekend schedule by day-of-week, then selects the band whose `start_minute` is the latest at or before the slot's minute-of-day — **wrapping past midnight** (before the earliest band's start, the previous day's last band carries over), so a schedule always covers 24h with no gaps regardless of ordering.

`select_band` returns `None` when the applicable schedule is empty; `buy_price` / `sell_price` then fall back to the flat `distribution_fee` / `sell_distribution_fee` on the config (also used when `local_tz` is `None`). This preserves the pre-TOU behaviour for installs that never define bands. `pipeline/tariff.py:bands_from_config` parses the stored config dicts (`{"start": "HH:MM", "buy_fee", "sell_fee"}`) into a sorted `TariffBand` tuple; the coordinator calls it for both schedules at setup.

The config/options flow collects up to `MAX_TARIFF_BANDS` (4) rows per schedule in the `tariff_weekday` / `tariff_weekend` steps; a row with a blank start time is dropped.

---

## 6. Resolution handling

- The price-feed translator is the single source of truth: resolution is detected from sensor data and recorded on `PriceFeedData.resolution`.
- `build_price_series_72h` forwards that value to `build_price_series` via the `resolution` argument — **no rederivation** from slot deltas.
- `build_price_series` only falls back to slot-delta derivation when called without an explicit `resolution` (test helpers, direct callers).

This matters when yesterday has fewer slots than today, or when the input is sparse: trusting `PriceFeedData.resolution` avoids contradictions between the two layers.

---

## 7. Integration in the DAG

`PricingNode` (`pipeline/nodes/tier1.py`):

```python
output_type = PriceSeries
consumes = [PriceFeedData, YesterdayPrices]
```

Tiers are not declared — `DagEngine._assign_tiers` derives them from `consumes`. Both inputs are primary (no upstream node produces them), so `PricingNode` lands in tier 1, in parallel with `BatteryStateNode`.

Coordinator flow (`orchestration/coordinator.py:_async_update_data`):

1. Run translators → `primary[PriceFeedData]`.
2. Build `YesterdayPrices` from the persistent store (empty tuple if the stored date is not yesterday) → `primary[YesterdayPrices]`.
3. `DagEngine.run(primary, ...)` → `PricingNode` calls `build_price_series_72h(...)` → `secondary[PriceSeries]`.
4. After the cycle, today's entries (filtered out of the feed entries by date) are written to the store as the next cycle's yesterday.

The coordinator no longer mutates the feed entries; its only role is to load/save the persistent yesterday slice.

---

## 8. Tests

All pricing tests live in `tests/test_pricing.py` and run under pure-Python pytest (no HA harness needed). Shared fixtures come from `tests/conftest.py` — `BASE_DT`, `make_price()`, and `default_tariff_config()`.

Run them with:

```bash
./venv/bin/python -m pytest tests/test_pricing.py -v
```

### Coverage

| Group | Test | What it checks |
|---|---|---|
| Round-trip basics | `test_empty_prices_returns_empty_series` | Empty input → empty `slots` tuple |
| | `test_slot_count_matches_input` | 24 entries → 24 slots |
| | `test_computed_at_is_set` | `computed_at == now` |
| | `test_sources_tuple` | Provenance fixed to `("nordpool", "tariff")` |
| Tariff math | `test_buy_price_formula` | Buy = `(spot + distribution + markup) * (1 + tax)` |
| | `test_sell_price_formula` | Sell = `(spot - dist - markup) * (1 - tax)` |
| | `test_spot_price_preserved` | Raw spot retained on slot |
| Sell sign | `test_negative_spot_produces_negative_sell` | Negative spot → `sell_eur_kwh < 0` |
| | `test_positive_spot_produces_positive_sell` | Positive spot → `sell_eur_kwh > 0` |
| | `test_sell_price_can_be_exactly_zero` | Fees that exactly cancel spot → `sell_eur_kwh == 0.0` |
| Resolution | `test_hourly_resolution_detected` | Hourly slot stride → `timedelta(hours=1)` |
| | `test_single_slot_defaults_to_hourly_resolution` | Single slot → defaults to 1h when no `resolution` arg |
| Helpers | `test_slot_at_returns_correct_slot` | `slot_at(t)` picks the slot containing `t` |
| | `test_slot_at_returns_none_outside_range` | Out-of-range time → `None` |
| | `test_window_returns_overlapping_slots` | `window(t1, t2)` returns slots overlapping the half-open interval |
| **72h assembly** | `test_72h_combines_yesterday_today_tomorrow` | 24 + 24 + 24 entries → 72 slots, ordered, boundaries correct |
| | `test_72h_uses_nordpool_resolution_not_derived` | Sparse input + 15-min `NordpoolData.resolution` → series resolution = 15-min (not rederived to 1h) |
| | `test_72h_empty_yesterday_returns_only_today_tomorrow` | Empty yesterday tuple → 48 slots starting at today |
| | `test_72h_applies_tariff_to_all_segments` | Tariff applied uniformly across yesterday and today slots |
| TOU bands | `test_local_tz_selects_band_per_slot` | `local_tz` projects each slot to local time; day vs night band fee applied per slot |
| | `test_no_local_tz_uses_flat_fee_even_with_bands` | No `local_tz` → flat distribution fee, bands ignored |

Band selection itself (weekday/weekend, wrap-midnight, fallback, `bands_from_config` parsing) is unit-tested in `tests/test_tariff.py`.

### What is intentionally **not** covered here

- **End-to-end DAG wiring** (PricingNode within the engine) — exercised in `tests/test_coordinator.py` and indirectly through `tests/test_schedule.py` / `tests/test_calculation.py`, which import `build_price_series` to construct fixtures.
- **HA-side Nordpool parsing** (15-min vs legacy attributes, zero-fill of tomorrow) — covered in `NordpoolTranslator` tests, not in the pricing module's tests.
- **Persistent yesterday store I/O** — coordinator's responsibility; covered in `tests/test_coordinator.py`.
