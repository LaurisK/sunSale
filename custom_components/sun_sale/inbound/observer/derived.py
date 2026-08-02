"""Derived-power stage: consumption + losses observers.

Two **synthetic** sides — neither is read directly from a single sensor.
Instead each cycle composes a synchronised ``DerivedPowerSample`` from the
component primaries (AC port, backup port, signed grid net, PV, battery)
and persists it as a rolling history; the engine then averages the
per-slot consumption / losses values from those samples.

Formulas (in codebase sign conventions, clamped ≥ 0):

  * **consumption_kw** = ``backup + ac_port_signed + grid_net_signed``

      where ``ac_port_signed`` is positive=inverter→grid (raw Solis
      convention) and ``grid_net_signed`` is positive=import (sunSale
      convention). Together they form the home-AC bus energy balance:
      anything the inverter contributes plus anything pulled from the
      grid lands on the home loads. Backup adds the (typically zero)
      outage-loop contribution.

  * **losses_kw** = ``solar − battery_signed − ac_port_signed − backup``

      where ``battery_signed`` is positive=charging (sunSale convention)
      — so charging is a DC-bus sink and is subtracted from solar to
      yield "DC power available for AC conversion". The two AC-output
      paths (AC port + backup) sum to the AC delivered; the leftover
      is conversion + cabling loss.

Bake-in: not wired for either side in this phase. Today and yesterday
are both raw averaged; ``baked_history`` is accepted but currently
ignored. The hook is kept so a future phase can plug a household-
consumption yesterday-total source into the existing bake-in pipeline
without touching the series-builder surface.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from datetime import tzinfo as TzInfo
from typing import Any

from ...contract.models import (
    AcPortPowerReading,
    BackupPowerReading,
    BakedObservedHistory,
    BatteryReading,
    DerivedPowerHistory,
    DerivedPowerSample,
    ObservedConsumptionSeries,
    ObservedConsumptionSlot,
    ObservedLossesSeries,
    ObservedLossesSlot,
    PvPowerReading,
    SunSaleConfig,
)
from ..telemetry import (
    GenericCodec,
    HaTelemetryReader,
    TelemetrySignal,
    resolve_bindings,
)
from .engine import ObservedSeriesEngine, Side

# Side identifiers for the derived-power engine instance. Stable across the
# codebase — used as the canonical keys in engine output dicts and (if a
# future bake-in is wired) as ``side_id`` on persisted records.
CONSUMPTION_SIDE_ID = "consumption"
LOSSES_SIDE_ID = "losses"


# Maximum age (seconds) for a power-source state to be considered fresh.
# Mirrors the grid-power observer guard against silently-frozen meter chains.
_DERIVED_POWER_MAX_AGE_S = 180


def _consumption_extract(s: DerivedPowerSample) -> float:
    """Return non-negative home-consumption kW for one DerivedPowerSample.

    Formula:
        ``max(0, backup + ac_port_signed + grid_net_signed)``

    where ``ac_port_signed`` is positive=inverter→grid and
    ``grid_net_signed`` is positive=import. Clamping handles brief
    cross-cycle inconsistencies between the AC-port and grid sensors.

    Args:
        s: Cycle-synchronised derived-power sample.

    Returns:
        Instantaneous home consumption in kW (≥ 0).
    """
    return max(0.0, s.backup_kw + s.ac_port_kw_signed + s.grid_net_kw_signed)


def _losses_extract(s: DerivedPowerSample) -> float:
    """Return non-negative inverter-conversion-loss kW for one DerivedPowerSample.

    Formula:
        ``max(0, solar − battery_signed − ac_port_signed − backup)``

    where ``battery_signed`` is positive=charging (subtracts because
    charging is a sink on the DC bus). The result is the difference
    between DC available pre-conversion and AC delivered post-conversion.

    Args:
        s: Cycle-synchronised derived-power sample.

    Returns:
        Instantaneous inverter losses in kW (≥ 0).
    """
    return max(
        0.0,
        s.solar_kw
        - s.battery_kw_signed
        - s.ac_port_kw_signed
        - s.backup_kw,
    )


def _consumption_side() -> Side:
    """Return the Side spec for the consumption track.

    Returns:
        Side with ``extract: DerivedPowerSample → consumption kW``.
    """
    return Side(id=CONSUMPTION_SIDE_ID, extract=_consumption_extract)


def _losses_side() -> Side:
    """Return the Side spec for the inverter-losses track.

    Returns:
        Side with ``extract: DerivedPowerSample → losses kW``.
    """
    return Side(id=LOSSES_SIDE_ID, extract=_losses_extract)


def build_derived_engine(local_tz: TzInfo) -> ObservedSeriesEngine:
    """Return a two-side engine instance for consumption + losses.

    Both sides consume the same ``DerivedPowerHistory`` stream via the
    ``samples_by_side`` engine input (both keys point to the same tuple);
    the per-side extractors produce different scalar values.

    Args:
        local_tz: Local timezone for day-boundary handling.

    Returns:
        ``ObservedSeriesEngine`` registered with consumption + losses sides.
    """
    return ObservedSeriesEngine(
        [_consumption_side(), _losses_side()], local_tz=local_tz,
    )


def build_derived_power_sample(
    now: datetime,
    ac_port: AcPortPowerReading | None,
    backup: BackupPowerReading | None,
    battery: BatteryReading | None,
    pv: PvPowerReading | None,
) -> DerivedPowerSample | None:
    """Compose one cross-stream sample from this cycle's primary readings.

    All five inputs are required (the formulas depend on every term); when
    any is missing the cycle is dropped — partial samples would bias the
    per-slot mean asymmetrically.

    ``battery`` carries both the battery power and the signed grid net
    (positive = import) in its existing ``grid_power_kw`` field — sourced
    via ``InverterController.get_grid_power`` and already
    sunSale-convention-aligned. ``pv`` reports watts; converted to kW here.

    Args:
        now: Cycle timestamp (UTC).
        ac_port: AC-port signed power reading (positive=inverter→grid).
        backup: Backup-port magnitude reading.
        battery: Per-cycle battery telemetry — carries battery_power +
            grid_net_signed.
        pv: PV power reading in watts.

    Returns:
        Synchronised ``DerivedPowerSample``, or ``None`` when any input is
        missing.
    """
    if ac_port is None or backup is None or battery is None or pv is None:
        return None
    return DerivedPowerSample(
        timestamp=now,
        ac_port_kw_signed=ac_port.power_kw,
        backup_kw=max(0.0, backup.power_kw),
        grid_net_kw_signed=battery.grid_power_kw,
        solar_kw=max(0.0, pv.power_w) / 1000.0,
        battery_kw_signed=battery.power_kw,
    )


def build_observed_consumption_series(
    derived_history: DerivedPowerHistory,
    price_slots: tuple,
    now: datetime | None = None,
    local_tz: TzInfo = UTC,
    baked_history: BakedObservedHistory | None = None,
) -> ObservedConsumptionSeries:
    """Derive per-slot observed home consumption from cross-stream samples.

    Slots span yesterday 00:00 local → now. Today and yesterday are both
    raw power-averaged via the engine — no bake-in source is wired yet
    (``baked_history`` reserved for a future phase).

    Args:
        derived_history: Persisted cross-stream samples.
        price_slots: Price-grid slots defining the output resolution.
        now: Cycle timestamp; defaults to UTC now.
        local_tz: Local timezone for day-boundary calculations.
        baked_history: Reserved for future bake-in wiring; currently unused.

    Returns:
        ObservedConsumptionSeries covering yesterday 00:00 local → now,
        grid-aligned. Empty when no price grid or no samples.
    """
    if now is None:
        now = datetime.now(UTC)

    if not price_slots or not derived_history.samples:
        return ObservedConsumptionSeries(slots=(), computed_at=now)

    engine = build_derived_engine(local_tz)
    today_start = _day_start(now, local_tz)
    yesterday_start = today_start - timedelta(days=1)
    samples_by_side = {
        CONSUMPTION_SIDE_ID: derived_history.samples,
        LOSSES_SIDE_ID: derived_history.samples,
    }

    today_slots = engine.build_slots_for_window(
        samples_by_side=samples_by_side,
        price_slots=price_slots,
        window_start=today_start,
        window_end=now,
    )[CONSUMPTION_SIDE_ID]

    yesterday_slots = engine.build_slots_for_window(
        samples_by_side=samples_by_side,
        price_slots=price_slots,
        window_start=yesterday_start,
        window_end=today_start,
    )[CONSUMPTION_SIDE_ID]

    slots = tuple(
        ObservedConsumptionSlot(
            start=s.start,
            end=s.end,
            consumed_kwh=s.kwh,
            source="inverter_derived",
        )
        for s in (yesterday_slots + today_slots)
    )

    return ObservedConsumptionSeries(
        slots=slots,
        computed_at=now,
        total_yesterday_kwh=round(sum(s.kwh for s in yesterday_slots), 4),
        total_today_so_far_kwh=round(sum(s.kwh for s in today_slots), 4),
    )


def build_observed_losses_series(
    derived_history: DerivedPowerHistory,
    price_slots: tuple,
    now: datetime | None = None,
    local_tz: TzInfo = UTC,
    baked_history: BakedObservedHistory | None = None,
) -> ObservedLossesSeries:
    """Derive per-slot observed inverter losses from cross-stream samples.

    Slots span yesterday 00:00 local → now. Both yesterday and today are
    raw power-averaged — no authoritative "losses today total" counter
    exists on the inverter, so the bake-in path doesn't apply here.

    Args:
        derived_history: Persisted cross-stream samples.
        price_slots: Price-grid slots defining the output resolution.
        now: Cycle timestamp; defaults to UTC now.
        local_tz: Local timezone for day-boundary calculations.
        baked_history: Accepted for interface parity with the other
            observers; not used (no bake-in source for losses).

    Returns:
        ObservedLossesSeries covering yesterday 00:00 local → now,
        grid-aligned. Empty when no price grid or no samples.
    """
    if now is None:
        now = datetime.now(UTC)

    if not price_slots or not derived_history.samples:
        return ObservedLossesSeries(slots=(), computed_at=now)

    engine = build_derived_engine(local_tz)
    today_start = _day_start(now, local_tz)
    yesterday_start = today_start - timedelta(days=1)
    samples_by_side = {
        CONSUMPTION_SIDE_ID: derived_history.samples,
        LOSSES_SIDE_ID: derived_history.samples,
    }

    today_slots = engine.build_slots_for_window(
        samples_by_side=samples_by_side,
        price_slots=price_slots,
        window_start=today_start,
        window_end=now,
    )[LOSSES_SIDE_ID]

    yesterday_slots = engine.build_slots_for_window(
        samples_by_side=samples_by_side,
        price_slots=price_slots,
        window_start=yesterday_start,
        window_end=today_start,
    )[LOSSES_SIDE_ID]

    slots = tuple(
        ObservedLossesSlot(
            start=s.start,
            end=s.end,
            losses_kwh=s.kwh,
            source="inverter_derived",
        )
        for s in (yesterday_slots + today_slots)
    )

    return ObservedLossesSeries(
        slots=slots,
        computed_at=now,
        total_yesterday_kwh=round(sum(s.kwh for s in yesterday_slots), 4),
        total_today_so_far_kwh=round(sum(s.kwh for s in today_slots), 4),
    )


def _day_start(t: datetime, local_tz: TzInfo) -> datetime:
    """Return local midnight for t's local day, expressed as a UTC-aware datetime.

    Args:
        t: Any tz-aware datetime.
        local_tz: Timezone defining "local midnight".

    Returns:
        UTC-aware datetime of local midnight on t's local date.
    """
    local_t = t.astimezone(local_tz)
    local_midnight = local_t.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(UTC)


# ---------------------------------------------------------------------------
# Translators (HA-edge readers)
# ---------------------------------------------------------------------------


class AcPortPowerTranslator:
    """Reads the inverter's AC grid-port power sensor; produces AcPortPowerReading.

    Sign is preserved as published by the sensor (Solis convention:
    positive = inverter→grid). The freshness guard rejects stale states the
    same way the grid-power observer does — Modbus chains can silently
    freeze and continue to publish a stale numeric value.
    """

    output_type = AcPortPowerReading

    def __init__(
        self,
        entity_id: str,
        max_age_s: float = _DERIVED_POWER_MAX_AGE_S,
    ) -> None:
        """Initialise with the AC-port entity + freshness window.

        Args:
            entity_id: Entity ID of the signed AC grid-port power sensor.
            max_age_s: Maximum age (seconds) of ``state.last_updated`` for
                the reading to be considered fresh. ``float('inf')``
                disables the freshness check.
        """
        self._max_age_s = max_age_s
        self._reader = HaTelemetryReader(
            resolve_bindings({}, ac_port_power=entity_id), GenericCodec()
        )

    def parse(self, hass: Any, now: datetime | None = None) -> AcPortPowerReading | None:
        """Read the AC-port sensor and return a signed-kW snapshot.

        Sign is preserved by the ``AC_PORT_POWER`` codec transform — the same
        one the recorder resampler uses.

        Args:
            hass: Home Assistant instance.
            now: Snapshot timestamp; defaults to UTC now.

        Returns:
            AcPortPowerReading, or None on absent / unparseable / stale state.
        """
        if now is None:
            now = datetime.now(UTC)
        power_kw = self._reader.read(
            hass, TelemetrySignal.AC_PORT_POWER, now=now, max_age_s=self._max_age_s
        )
        if power_kw is None:
            return None
        return AcPortPowerReading(power_kw=power_kw, timestamp=now)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime,
    ) -> AcPortPowerReading | None:
        """DAG translator entry-point; delegates to ``parse``."""
        return self.parse(hass, now)


class BackupPowerTranslator:
    """Reads the inverter's backup-port output sensor; produces BackupPowerReading.

    The backup port only sources power, so the reading is clamped to ≥ 0
    in case a sensor briefly reports a small negative bias near zero.
    Freshness check mirrors ``AcPortPowerTranslator``.
    """

    output_type = BackupPowerReading

    def __init__(
        self,
        entity_id: str,
        max_age_s: float = _DERIVED_POWER_MAX_AGE_S,
    ) -> None:
        """Initialise with the backup-port entity + freshness window.

        Args:
            entity_id: Entity ID of the backup-port output power sensor.
            max_age_s: Maximum age (seconds) of ``state.last_updated`` for
                the reading to be considered fresh.
        """
        self._max_age_s = max_age_s
        self._reader = HaTelemetryReader(
            resolve_bindings({}, backup_power=entity_id), GenericCodec()
        )

    def parse(self, hass: Any, now: datetime | None = None) -> BackupPowerReading | None:
        """Read the backup-port sensor and return a non-negative-kW snapshot.

        The ``≥ 0`` clamp is the ``BACKUP_POWER`` codec transform — the same
        one the recorder resampler uses.

        Args:
            hass: Home Assistant instance.
            now: Snapshot timestamp; defaults to UTC now.

        Returns:
            BackupPowerReading with ``power_kw`` ≥ 0, or None on absent /
            unparseable / stale state.
        """
        if now is None:
            now = datetime.now(UTC)
        power_kw = self._reader.read(
            hass, TelemetrySignal.BACKUP_POWER, now=now, max_age_s=self._max_age_s
        )
        if power_kw is None:
            return None
        return BackupPowerReading(power_kw=power_kw, timestamp=now)

    async def translate(
        self, hass: Any, config: SunSaleConfig, raw_config: dict, now: datetime,
    ) -> BackupPowerReading | None:
        """DAG translator entry-point; delegates to ``parse``."""
        return self.parse(hass, now)
