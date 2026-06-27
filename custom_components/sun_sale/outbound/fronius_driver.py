"""Fronius Gen24 / Symo Advanced control driver — NOTES ONLY (not implemented).

This module is a deliberate placeholder. No ``EntityControlDriver`` is built for
Fronius today; selecting the platform yields telemetry-only behaviour (a no-op
``apply_mode`` via the default driver path). The notes below capture what a real
implementation must do.

=============================  OBSERVATIONS  ===================================
Why not entity-driven like the others
-------------------------------------
The **official HA ``fronius`` integration is read-only** — it exposes sensors
only, with no controllable ``number`` / ``select`` / ``switch`` entities for
battery control. So there is nothing for the shared ``EntityControlDriver`` to
write. Control is possible, but only through one of:

  1. **Raw SunSpec storage-model Modbus writes** (``modbus.write_register``
     against the inverter). The relevant registers (SunSpec model 124):
       * ``StorCtl_Mod`` (≈ reg 40348) — storage control bitfield (charge/
         discharge enable).
       * ``MinRsvPct``  (≈ 40350) — minimum reserve SoC %.
       * ``OutWRte``    (≈ 40355) — discharge rate %.
       * ``InWRte``     (≈ 40356) — charge rate %.
     "Forced Recharge" is reached by setting ``StorCtl_Mod`` + ``InWRte`` while
     raising ``MinRsvPct``. Requires "Inverter control via Modbus" enabled in the
     inverter web UI, and the exact base addresses shift with the SunSpec map
     offset (read the model header — do not hardcode).
  2. **A custom component** (redpomodoro/callifo ``fronius_modbus``) that wraps
     those registers as HA entities — if adopted, Fronius could become a normal
     ``EntityControlDriver`` with roles like ``storage_ctrl_mode`` /
     ``charge_rate_pct`` / ``discharge_rate_pct`` / ``min_reserve_pct``.

How a driver would map StorageModes (SunSpec model 124)
-------------------------------------------------------
  * ``SelfUse``   — StorCtl_Mod = 0 (auto), MinRsvPct at the floor.
  * ``NoExport``  — needs the inverter's export-limit (model 123 WMaxLim) or
    "Do not feed in" setting; not in model 124.
  * ``GridCharge``— StorCtl_Mod charge-bit set, InWRte = 100%, MinRsvPct raised
    toward target.
  * ``Discharge`` — StorCtl_Mod discharge-bit set, OutWRte = 100%.
  * ``StandBy``   — StorCtl_Mod with both rates at 0 (hold).

Open problems before implementing
---------------------------------
  * Rates are **percentages of rated power**, not watts — convert against the
    inverter rating, and the dispatcher's W setpoints must be normalised.
  * No clean string mode readback — ``decode_observed`` would have to infer the
    mode from StorCtl_Mod + the rate registers (closer to the Solis decoder than
    the entity-driven ones).
  * Whether writes go to a holding register that survives reboot (EEPROM wear)
    is model-dependent — verify before per-slot writing.
  * Needs its own Modbus client (the driver layer has none); simplest path is to
    adopt the custom component and treat it as an entity-driven platform.
================================================================================
"""
from __future__ import annotations

# Intentionally no driver factory — see module docstring. The platform resolves
# to the telemetry-only default path in ``make_inverter_driver``.
