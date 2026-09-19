# sunSale

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/)
[![license](https://img.shields.io/badge/License-AGPL--3.0--or--later-blue.svg)](LICENSE)

Home Assistant integration that automates electricity **buying and selling** for homes with
solar + battery storage. It runs a SoC-bucketed dynamic-programming scheduler over Nordpool
spot prices, your tariff, the solar forecast, a learned household baseload, battery state, and
degradation cost — then drives the inverter to the most profitable storage mode each slot.

> **Status: alpha (v0.1.0).** Only the **Solis** platform (`solis_modbus`) is verified on real
> hardware. The Huawei / SolaX / Sungrow / GoodWe / Deye drivers are implemented and selectable but
> **untested against hardware** — verify on first connect. Fronius / SMA / Kostal are telemetry-only
> (no write). Run with the **Automation** switch off first to observe before trusting it.

Everything flows through other HA integrations — sunSale calls no external API directly.

## Requirements

- Home Assistant 2024.x+ (Python 3.12+)
- A price source — [Nordpool](https://github.com/custom-components/nordpool) (default), or ENTSO-e /
  Octopus Agile / Amber, selectable in setup — or none when both your buy and sell prices are fixed.
  Currency follows the source (EUR/SEK/NOK/DKK/GBP/AUD/…); the optimisation math is currency-neutral.
- An inverter/battery integration — for Solis: [Pho3niX90/solis_modbus](https://github.com/Pho3niX90/solis_modbus)
- *(optional)* a solar forecast integration (Forecast.Solar, Solcast, Open-Meteo, …)

## Install

**HACS (custom repository):** HACS → Integrations → ⋮ → *Custom repositories* → add
`https://github.com/LaurisK/sunSale` as category **Integration** → Download → restart HA.

**Manual:** copy `custom_components/sun_sale/` into `config/custom_components/`, restart HA.

Then **Settings → Devices & Services → Add Integration → sunSale**.

## Configure

Setup opens on one menu listing the sections — **Electricity prices**, **Inverter and battery**
and **Solar forecast** (once an inverter is set up). Open a section to add it to the installation;
each section is a menu of pages (price source and sensor, buy price, sell price, price level,
price-forecast weather; platform and power ratings, battery, entity mapping, dedicated BMS, power
sensors, energy counters). Every page is saved with its **Confirm** button, whose last field is a
dropdown offering **✓ save and go back** or **✕ discard and go back**. **✓ Finish configuration**
saves once every section you added is complete. For Solis, the `solis_modbus` config entry is
auto-detected and all entity IDs are resolved from the HA registry — manual mapping is only a
fallback. **Configure** on the integration card opens the same menus later. Effective prices used
by the scheduler:

```
buy  = (energy + markup + grid_fee) × (1 + VAT)
sell = (energy − deduction − grid_fee) × (1 − tax)
```

Buy and sell are set up independently. *energy* is the market price from the price source (a
market-based sell price uses a separate live export feed where the source has one) or a fixed
price you enter. The grid fee has **1, 2 or 4 tariffs**: with one it applies at all times; with 2
or 4 you enter a fee per tariff and, per day type, which tariff is active from what time (up to
six "from HH:MM → tariff" rows, 24 h, wrapping past midnight). Workdays always have a schedule;
weekends and public holidays (the national calendar of Home Assistant's country) can get their
own, and summer and winter can have separate schedules with configurable start dates.

A trade is taken only when `sell × efficiency − buy − 2 × degradation > 0`, where degradation =
`purchase_price / (rated_cycle_life × estimated_capacity × 2)`. Capacity is learned over days
from the inverter's energy counters.

## Entities

Created under one `sunSale` device. Highlights (full set in [`docs/`](docs/)):

- **Actions/prices:** `sensor.sunsale_current_action`, `..._next_action[_time]`,
  `..._current_buy_price` / `..._current_sell_price`, `..._expected_profit_today`, `..._schedule`.
- **Battery/diagnostics:** `..._estimated_battery_capacity`, `..._degradation_cost`,
  `..._current_baseload`, `..._battery_runtime`, `..._monthly_bill`,
  `..._observed_inverter_mode` (commanded vs. observed + per-register verify state).
- **Energy counters:** `..._today_{generation,imported,exported}` and the baked `..._yesterday_*`.
- **Controls:** `select.sunsale_mode_override` (operator intent; `sunSale` sentinel hands back to
  the scheduler), `switch.sunsale_automation` (master kill-switch), the `switch.sunsale_allow_*` /
  `..._use_standby` policy switches, and the `number.sunsale_*` planner knobs.
- **Services:** `sun_sale.force_recalculate`, `sun_sale.force_verify_inverter_mode`.
  The coordinator otherwise refreshes every 5 minutes.

**Mode dispatch** is write-once-on-change → verify → reconcile (no per-cycle re-assert); RC-backed
modes refresh the RAM-only RC registers each cycle. Contract in
[`docs/control_loop.md`](docs/control_loop.md) and [`docs/solis_control.md`](docs/solis_control.md).

## Recommended rollout

1. Install/configure with **Automation off**; watch `sensor.sunsale_schedule` for a day or two.
2. Confirm a manual `select.sunsale_mode_override` reaches the inverter (`..._observed_inverter_mode`).
3. Turn **Automation on**; tune tariff/battery/policy values as real-world cost becomes clear.

## Development

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements_dev.txt
pytest                                   # full suite (mostly HA-free)
ruff check custom_components/sun_sale/ && mypy custom_components/sun_sale/
```

`contract/`, `pipeline/`, and the pure helpers run without Home Assistant imports.
Architecture (tiered observer DAG → control module) is in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/MODULES.md`](docs/MODULES.md).
Contributing (incl. DCO sign-off): [`CONTRIBUTING.md`](CONTRIBUTING.md).

**Live tools** (`tools/`) target a running HA via REST. Credentials resolve from `--url`/`--token`,
the `HA_URL`/`HA_TOKEN` env vars, or a local `tools/secrets.json` (gitignored — copy
`tools/secrets.example.json`):

- `tools/integration_check.py` — deep-checks a live `/api/sun_sale/debug` snapshot against raw
  entity state; exits non-zero on failure (CI / post-deploy gating).
- `tools/deploy.py` — push branch → force HACS redownload → restart HA → wait for reload.

## Known limitations

- Only `solis_modbus` is hardware-verified; the other vendor drivers are implemented but untested
  against hardware (verify on first connect), and Fronius / SMA / Kostal are telemetry-only.
- The SoC-bucketed DP introduces small discretisation error vs. a continuous optimum.

## License

[GNU AGPL-3.0-or-later](LICENSE) © 2026 Laurynas Klimavicius.

Free to use, modify, and redistribute — **as long as your version stays open under the same
license.** Because sunSale can control hardware over a network, the AGPL also requires anyone
who runs a modified version as a network-accessible service to make their source available.
