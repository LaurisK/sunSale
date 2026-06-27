"""Kostal Plenticore control driver — NOTES ONLY (not implemented).

Deliberate placeholder; no ``EntityControlDriver`` is built. Selecting the
Kostal platform yields telemetry-only behaviour. Notes below capture what a real
implementation needs.

=============================  OBSERVATIONS  ===================================
What the official integration gives us (partial)
------------------------------------------------
The HA core ``kostal_plenticore`` integration **does** expose some writable
control — unlike Fronius/SMA:
  * ``select`` "Battery charging / usage mode": ``None`` /
    ``Battery:SmartBatteryControl:Enable`` / ``Battery:TimeControl:Enable``.
  * Several writable ``number`` entities (min SoC, etc.).

So the passive modes are reachable via entities, but the **forced** modes are
not directly:
  * There is **no real-time "force charge from grid at X W now" primitive** in
    the standard select. Time-control schedules charging windows; smart control
    optimises around feed-in — neither is an immediate setpoint.
  * True per-slot forcing requires the inverter's **"external battery control
    via Modbus TCP"** mode, which exposes a writable charge/discharge **power
    setpoint** register. That mode must be enabled on the device, and the
    setpoint registers are written over raw Modbus (not the REST-based core
    integration).

How a driver would map StorageModes
-----------------------------------
Passive (via the core integration's entities):
  * ``SelfUse``  — usage mode = ``Battery:SmartBatteryControl:Enable``.
  * ``NoExport`` — needs the AC feed-in / external-control export limit.
  * ``StandBy``  — usage mode ``None`` + min-SoC pinned (approximate hold).

Forced (require the external-control Modbus power setpoint — NOT in core):
  * ``GridCharge`` — external control, charge setpoint = max charge W.
  * ``Discharge``  — external control, discharge setpoint = inverter max W.

Path to a real driver
----------------------
Two-tier:
  1. Entity-driven for the passive modes (works with core
     ``kostal_plenticore`` today — could be a partial ``EntityControlDriver``
     with roles ``usage_mode_select`` / ``min_soc``).
  2. A raw-Modbus extension for the forced setpoint, gated on the device's
     external-battery-control mode being enabled. The driver layer has no Modbus
     client, so this needs either a custom component or new plumbing.

Open problems before implementing
---------------------------------
  * Mixing REST (core integration) reads with raw-Modbus writes risks readback
    skew in the verify loop — pick one transport per control point.
  * Confirm the external-control setpoint registers are RAM-backed before
    per-slot writing.
  * Without external-control mode, GridCharge/Discharge are simply unavailable —
    a partial driver should advertise a reduced ``DISPATCHABLE_MODES`` set
    rather than silently no-op the forced modes.
================================================================================
"""
from __future__ import annotations

# Intentionally no driver factory — see module docstring.
