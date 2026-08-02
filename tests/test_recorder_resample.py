"""Tests for inbound/observer/recorder_resample.py — pure Python, no HA.

The recorder-resampling builders duplicate the per-sample math of the live
translators / ``InverterController`` (unit normalisation, the Solis battery /
grid sign-flips, the directional split, the all-or-nothing derived gate).
These tests pin that math against the live behaviour so the two paths can't
drift, and cover the forward-fill union-timeline composition + the merge.
"""
import asyncio
from datetime import UTC, datetime, timedelta

import custom_components.sun_sale.inbound.observer.recorder_resample as rr
from custom_components.sun_sale.contract.models import (
    AcPortPowerReading,
    BackupPowerReading,
    BatteryReading,
    PvPowerReading,
)
from custom_components.sun_sale.inbound.observer.derived import (
    _consumption_extract,
    _losses_extract,
    build_derived_power_sample,
)
from custom_components.sun_sale.inbound.observer.recorder_resample import (
    RecorderResampler,
    ResampleSources,
    build_derived_samples,
    build_grid_readings,
    build_pv_readings,
    merge_by_timestamp,
)
from custom_components.sun_sale.inbound.telemetry import GenericCodec, SolisCodec

T0 = datetime(2026, 6, 19, 10, 0, 0, tzinfo=UTC)


def _t(seconds: int) -> datetime:
    """Return T0 offset by ``seconds``."""
    return T0 + timedelta(seconds=seconds)


def _m(minutes: int) -> datetime:
    """Return T0 offset by ``minutes``."""
    return T0 + timedelta(minutes=minutes)


def _pv_only_sources() -> ResampleSources:
    """Return sources with only a PV sensor mapped (other streams empty)."""
    return ResampleSources(
        pv_power="sensor.pv",
        ac_port_power="",
        backup_power="",
        grid_import_power="",
        grid_export_power="",
        signed_grid_power="",
        entity_ids={},
        codec=GenericCodec(),
    )


# --- PV --------------------------------------------------------------------

def test_build_pv_readings_unit_handling_and_clamp():
    """W passes through, kW scales ×1000, negatives clamp to 0."""
    series = [
        (_t(0), 1500.0, "W"),
        (_t(30), 2.0, "kW"),
        (_t(60), -50.0, "W"),
    ]
    out = build_pv_readings(series, GenericCodec())
    assert [r.power_w for r in out] == [1500.0, 2000.0, 0.0]
    assert [r.timestamp for r in out] == [_t(0), _t(30), _t(60)]


# --- Grid ------------------------------------------------------------------

def test_build_grid_readings_signed_split():
    """Signed net splits per sample: import = max(0, net), export = max(0, -net)."""
    signed = [
        (_t(0), 1.5, "kW"),     # importing
        (_t(30), -2.0, "kW"),   # exporting
        (_t(60), 0.0, "kW"),    # idle
    ]
    imports, exports = build_grid_readings(
        [], [], signed, has_directional=False, codec=GenericCodec(),
    )
    assert [r.power_kw for r in imports] == [1.5, 0.0, 0.0]
    assert [r.power_kw for r in exports] == [0.0, 2.0, 0.0]


def test_build_grid_readings_directional_clamps_each():
    """Directional sensors are normalised + clamped ≥ 0 independently."""
    imp = [(_t(0), 1200.0, "W"), (_t(30), -10.0, "W")]
    exp = [(_t(0), 0.3, "kW")]
    imports, exports = build_grid_readings(
        imp, exp, [], has_directional=True, codec=GenericCodec(),
    )
    assert [r.power_kw for r in imports] == [1.2, 0.0]
    assert [r.power_kw for r in exports] == [0.3]


# --- Derived ---------------------------------------------------------------

def test_build_derived_matches_live_composition_solis():
    """A single aligned instant matches build_derived_power_sample exactly.

    Uses the Solis signed roles so the battery/grid sign-flips are exercised:
    battery_power_net is discharge-positive, grid_power_net is import-positive.
    """
    solar = [(_t(0), 3000.0, "W")]      # 3.0 kW PV
    ac = [(_t(0), 1.2, "kW")]           # +1.2 inverter→grid
    bu = [(_t(0), 0.1, "kW")]           # 0.1 backup
    bat = [(_t(0), 2.5, "kW")]          # net discharge-positive 2.5 → -2.5 charging-conv
    grid = [(_t(0), 0.4, "kW")]         # +0.4 import (sunSale)
    soc = [(_t(0), 55.0, "%")]

    out = build_derived_samples(
        solar, ac, bu, bat, grid, soc,
        battery_is_signed_role=True, grid_is_fallback=False, codec=SolisCodec(),
    )
    assert len(out) == 1
    s = out[0]

    # The live path: signed battery role on Solis flips sign (discharge+ → −).
    live = build_derived_power_sample(
        now=_t(0),
        ac_port=AcPortPowerReading(power_kw=1.2, timestamp=_t(0)),
        backup=BackupPowerReading(power_kw=0.1, timestamp=_t(0)),
        battery=BatteryReading(
            soc=0.55, power_kw=-2.5, grid_power_kw=0.4, household_load_kw=0.0,
        ),
        pv=PvPowerReading(power_w=3000.0, timestamp=_t(0)),
    )
    assert s.ac_port_kw_signed == live.ac_port_kw_signed == 1.2
    assert s.backup_kw == live.backup_kw == 0.1
    assert s.grid_net_kw_signed == live.grid_net_kw_signed == 0.4
    assert s.solar_kw == live.solar_kw == 3.0
    assert s.battery_kw_signed == live.battery_kw_signed == -2.5

    # The downstream extracts agree too.
    assert _consumption_extract(s) == _consumption_extract(live)
    assert _losses_extract(s) == _losses_extract(live)


def test_build_derived_forward_fills_union_timeline():
    """A sample is emitted at every signal change, others held at last value."""
    solar = [(_t(0), 1000.0, "W"), (_t(60), 2000.0, "W")]
    ac = [(_t(30), 0.5, "kW")]
    bu = [(_t(0), 0.0, "kW")]
    bat = [(_t(0), 0.0, "kW")]
    grid = [(_t(0), 0.0, "kW")]
    soc = [(_t(0), 50.0, "%")]

    out = build_derived_samples(
        solar, ac, bu, bat, grid, soc,
        battery_is_signed_role=False, grid_is_fallback=False, codec=GenericCodec(),
    )
    # Union timeline = {0, 30, 60}. At t=0 ac has no value yet → gated out.
    # At t=30 ac appears (solar still 1.0 kW). At t=60 solar updates to 2.0.
    assert [s.timestamp for s in out] == [_t(30), _t(60)]
    assert out[0].solar_kw == 1.0 and out[0].ac_port_kw_signed == 0.5
    assert out[1].solar_kw == 2.0 and out[1].ac_port_kw_signed == 0.5


def test_build_derived_gated_until_soc_present():
    """No derived sample until battery SoC has a value (mirrors live drop)."""
    solar = [(_t(0), 1000.0, "W")]
    ac = [(_t(0), 0.5, "kW")]
    bu = [(_t(0), 0.0, "kW")]
    bat = [(_t(0), 0.0, "kW")]
    grid = [(_t(0), 0.0, "kW")]
    soc = [(_t(30), 50.0, "%")]    # SoC only appears at t=30

    out = build_derived_samples(
        solar, ac, bu, bat, grid, soc,
        battery_is_signed_role=False, grid_is_fallback=False, codec=GenericCodec(),
    )
    assert [s.timestamp for s in out] == [_t(30)]


def test_build_derived_empty_when_a_signal_missing():
    """An entirely-absent contributing signal yields no samples."""
    series = [(_t(0), 1.0, "kW")]
    out = build_derived_samples(
        series, series, series, series, series, [],   # soc empty
        battery_is_signed_role=False, grid_is_fallback=False, codec=GenericCodec(),
    )
    assert out == []


def test_grid_fallback_sign_flip_solis():
    """grid_net from the ac-port fallback role flips sign on Solis."""
    grid = [(_t(0), 1.0, "kW")]     # ac_grid_port_power: +1.0 inverter→grid
    out = build_derived_samples(
        [(_t(0), 0.0, "W")], [(_t(0), 0.0, "kW")], [(_t(0), 0.0, "kW")],
        [(_t(0), 0.0, "kW")], grid, [(_t(0), 50.0, "%")],
        battery_is_signed_role=False, grid_is_fallback=True, codec=SolisCodec(),
    )
    # +1.0 inverter→grid means exporting → grid net (import-positive) = −1.0.
    assert out[0].grid_net_kw_signed == -1.0


# --- Merge -----------------------------------------------------------------

def test_merge_by_timestamp_dedups_and_sorts():
    """Existing ∪ resampled, deduped by timestamp, time-sorted."""
    a = [PvPowerReading(power_w=1.0, timestamp=_t(60))]
    b = [
        PvPowerReading(power_w=2.0, timestamp=_t(0)),
        PvPowerReading(power_w=3.0, timestamp=_t(60)),   # duplicate ts — dropped
        PvPowerReading(power_w=4.0, timestamp=_t(30)),
    ]
    merged = merge_by_timestamp(a, b)
    assert [r.timestamp for r in merged] == [_t(0), _t(30), _t(60)]
    # First occurrence of the duplicate ts (from `a`) wins.
    assert merged[2].power_w == 1.0


# --- RecorderResampler (stateful, incremental) -----------------------------

def _patch_fetch(monkeypatch, data: dict, calls: list):
    """Patch the module's fetch_series with a recorder stub.

    Returns the rows in ``data[entity_id]`` whose timestamp is within
    ``[start, end]`` (mimicking ``include_start_time_state`` loosely) and
    records each ``(entity_id, start, end)`` in ``calls``.
    """
    async def fake_fetch(hass, entity_id, start, end):
        calls.append((entity_id, start, end))
        return [r for r in data.get(entity_id, []) if start <= r[0] <= end]

    monkeypatch.setattr(rr, "fetch_series", fake_fetch)


def test_resampler_bootstrap_then_incremental_window(monkeypatch):
    """First update fetches from window_start; later updates fetch only the delta."""
    data = {"sensor.pv": [(_m(0), 1000.0, "W"), (_m(30), 1500.0, "W"), (_m(60), 2000.0, "W")]}
    calls: list = []
    _patch_fetch(monkeypatch, data, calls)
    r = RecorderResampler(_pv_only_sources())

    out1 = asyncio.run(r.update(None, _m(0), _m(30)))
    assert calls[-1] == ("sensor.pv", _m(0), _m(30))          # bootstrap from floor
    assert [x.power_w for x in out1["pv"]] == [1000.0, 1500.0]

    out2 = asyncio.run(r.update(None, _m(0), _m(60)))
    # Incremental: fetch_start = last_end(_m30) − 10 min overlap = _m20.
    assert calls[-1] == ("sensor.pv", _m(20), _m(60))
    assert [x.power_w for x in out2["pv"]] == [1000.0, 1500.0, 2000.0]


def test_resampler_trims_to_window(monkeypatch):
    """Samples older than the advancing window floor are dropped from the buffer."""
    data = {"sensor.pv": [
        (_m(0), 1000.0, "W"), (_m(30), 1500.0, "W"),
        (_m(60), 2000.0, "W"), (_m(70), 2500.0, "W"),
    ]}
    calls: list = []
    _patch_fetch(monkeypatch, data, calls)
    r = RecorderResampler(_pv_only_sources())

    asyncio.run(r.update(None, _m(0), _m(30)))
    asyncio.run(r.update(None, _m(0), _m(60)))
    out = asyncio.run(r.update(None, _m(50), _m(70)))   # floor jumps to _m50
    assert [x.timestamp for x in out["pv"]] == [_m(60), _m(70)]


def test_resampler_overlap_recovers_failed_cycle(monkeypatch):
    """A cycle that fetched nothing (recorder hiccup) is healed by the next overlap."""
    data = {"sensor.pv": [(_m(0), 1000.0, "W"), (_m(12), 1200.0, "W"), (_m(25), 2500.0, "W")]}
    calls: list = []
    _patch_fetch(monkeypatch, data, calls)
    r = RecorderResampler(_pv_only_sources())

    asyncio.run(r.update(None, _m(0), _m(10)))             # buffer = {_m0}

    # Recorder fails this cycle → no rows; the _m12 point is missed for now.
    async def fail_fetch(hass, entity_id, start, end):
        calls.append((entity_id, start, end))
        return []
    monkeypatch.setattr(rr, "fetch_series", fail_fetch)
    out_fail = asyncio.run(r.update(None, _m(0), _m(20)))
    assert [x.timestamp for x in out_fail["pv"]] == [_m(0)]

    # Recorder recovers; fetch_start = last_end(_m20) − 10 min = _m10, so _m12
    # is re-read and back-filled.
    _patch_fetch(monkeypatch, data, calls)
    out_ok = asyncio.run(r.update(None, _m(0), _m(30)))
    assert [x.timestamp for x in out_ok["pv"]] == [_m(0), _m(12), _m(25)]


def test_resampler_omits_streams_without_data(monkeypatch):
    """Unmapped / empty streams are simply absent from the result dict."""
    _patch_fetch(monkeypatch, {}, [])
    r = RecorderResampler(_pv_only_sources())
    out = asyncio.run(r.update(None, _m(0), _m(30)))
    assert out == {}
