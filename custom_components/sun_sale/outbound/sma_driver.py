"""SMA Sunny Tripower Smart Energy control driver — NOTES ONLY (not implemented).

Deliberate placeholder; no ``EntityControlDriver`` is built. Selecting the SMA
platform yields telemetry-only behaviour. Notes below capture the path a real
implementation must take.

=============================  OBSERVATIONS  ===================================
Why this is the hardest of the targets
--------------------------------------
  * The **official HA ``sma`` integration is read-only** (Speedwire/webconnect
    sensors) — no control entities.
  * SMA's hybrid inverters are architected around the **Sunny Home Manager 2.0**
    as the energy manager. External battery control via Modbus is possible but
    documented as finicky and is intended to *override* the home manager, not
    cooperate with it. Grid-charge in particular fights the SHM's own logic.
  * SMA Modbus registers are partly EEPROM-backed and SMA explicitly warns
    against frequent writes (limited write endurance) — at odds with a per-slot
    dispatcher. Any implementation must lean hard on the actuator's idempotent
    "skip if unchanged" and avoid thrashing.

Control path (raw Modbus, no HA entities)
-----------------------------------------
External power control uses the battery-control registers, roughly:
  * ``BatChaMaxW`` / ``BatChaMinW`` style limits, plus an external-control
    activation/grid-setpoint register pair (the "BMS operating mode" /
    "external setpoint" group). The exact addresses depend on device and the
    Modbus profile downloaded from SMA's portal — they are NOT stable across
    models, so read SMA's per-device "Parameters / Modbus" file.
  * Forced grid charge ≈ set the external charge setpoint (negative = charge)
    after switching the battery operating mode to external control.

How a driver would map StorageModes (sketch)
--------------------------------------------
  * ``SelfUse``   — battery operating mode = self-consumption (release external
    control).
  * ``GridCharge``— external control on, charge setpoint = max charge W.
  * ``Discharge`` — external control on, discharge setpoint = inverter max W.
  * ``NoExport``  — grid-feed limit register / "balanced to zero" mode.
  * ``StandBy``   — external control on, setpoint 0.

Open problems before implementing
---------------------------------
  * **EEPROM write endurance** — possibly disqualifying for 15-min dispatch
    unless the external-setpoint registers are RAM-backed (CONFIRM per model).
  * No HA control entities → needs its own Modbus client (the driver layer has
    none) or a community custom component exposing the registers as entities.
  * Coexistence with Sunny Home Manager is unresolved; may require physically
    removing the SHM from the EMS role.
  * Recommend treating SMA as **out of scope** until a maintained
    control-capable HA integration exists.
================================================================================
"""
from __future__ import annotations

# Intentionally no driver factory — see module docstring.
