/* Sun Sale Dashboard Panel
 *
 * Daily generation row (above main chart):
 *   Compact text pills — 8 days: yesterday → +6 days.
 *   Yesterday/Today show predicted / actual (today also shows remaining-forecast).
 *   Future days show predicted only. Data: forecast_daily_kwh,
 *   actual_yesterday_kwh, actual_today_kwh.
 *
 * 72-hour window: yesterday 00:00 → tomorrow 23:59 (local)
 *
 * Left Y axis — currency/kWh (label follows the configured CONF_CURRENCY):
 *   Buy price   amber  solid stepline  — past history + future from pricing sensor (one continuous line)
 *   Sell price  coral  solid stepline  — past history + future from pricing sensor (one continuous line)
 *
 * Right Y axis — kWh/slot (15-min):
 *   Solar forecast — vertical bar per 15-min slot (scalar y = forecast_kwh), grey 25 % opacity.
 *
 *   Forecast-vs-observed error overlay — green/red rect per slot, anchored
 *   at the top of the forecast bar (y = forecast_kwh):
 *     +error (observed > forecast)  → green segment grows UP
 *     -error (observed < forecast)  → red segment grows DOWN
 *   Rendered as an ECharts custom series so it survives zoom/pan natively.
 *
 * Overlays:
 *   Dashed white line "Now"
 *   Dashed grey line  y = 0 price reference
 */

(function () {
  'use strict';

  // ── Entity IDs ─────────────────────────────────────────────────────────────
  // Update these to match your Home Assistant instance.
  const BUY_PRICE_ENTITY  = 'sensor.sunsale_current_buy_price';
  const SELL_PRICE_ENTITY = 'sensor.sunsale_current_sell_price';
  const PRICING_ENTITY    = 'sensor.sunsale_pricing';
  const DASHBOARD_ENTITY      = 'sensor.sunsale_dashboard';
  const MONTHLY_BILL_ENTITY   = 'sensor.sunsale_monthly_bill';
  const ECHARTS_CDN           = 'https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js';

  // ── Helpers ────────────────────────────────────────────────────────────────

  function localMidnight(offsetDays) {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() + offsetDays);
    return d.getTime();
  }

  function loadECharts() {
    if (window.echarts) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = ECHARTS_CDN;
      s.onload = resolve;
      s.onerror = () => reject(new Error('Could not load ECharts from CDN'));
      document.head.appendChild(s);
    });
  }

  // ── Panel element ──────────────────────────────────────────────────────────

  class SunSalePanel extends HTMLElement {
    constructor() {
      super();
      this._hass         = null;
      // Per-entry entity resolution. Default to the bare (single-install) ids;
      // _scopeEntitiesToEntry() narrows them to this panel's config entry once
      // hass + panel.config.entry_id are available.
      this._dashboardEid   = DASHBOARD_ENTITY;
      this._monthlyBillEid = MONTHLY_BILL_ENTITY;
      this._pricingEid     = PRICING_ENTITY;
      this._buyPriceEid    = BUY_PRICE_ENTITY;
      this._sellPriceEid   = SELL_PRICE_ENTITY;
      this._entryEntityIds = null;   // Set of this entry's entity_ids, or null = no scoping
      this._eidByKey       = null;   // logical key (sunsale_<key>) → scoped entity_id
      this._chart        = null;
      this._g1Chart      = null;
      this._g2Chart      = null;
      this._g3Chart      = null;
      this._qualityCharts = [];   // [{chart, el, title, labels, metrics}] for resize
      this._resizeObs    = null;
      this._resizeRaf    = 0;
      this._lastResizeW  = -1;
      this._initialized  = false;
      // Display currency code + symbol, refreshed from the dashboard sensor each
      // hass update (see _updateCurrency). Affects only labels — never any math.
      this._currency       = 'EUR';
      this._currencySymbol = '€';
      this.attachShadow({ mode: 'open' });
    }

    set hass(hass) {
      this._hass = hass;
      this._scopeEntitiesToEntry(hass);
      this._updateCurrency(hass);
      if (!this._initialized) {
        this._initialized = true;
        this._boot();
      } else {
        const dashAttrs = hass.states[this._dashboardEid]?.attributes;
        this._renderBatteryRow(dashAttrs);
        this._renderGenerationRow(dashAttrs);
        this._renderFlowDiagram(dashAttrs);
        const panel = this.shadowRoot?.querySelector('#schedule-panel');
        if (panel?.classList.contains('open')) this._syncScheduleDrawer();
        this._maybeRefreshCharts(hass);
      }
    }

    // Refresh the display currency code + symbol from the dashboard sensor's
    // `currency` attribute (server-resolved from CONF_CURRENCY). Symbols are a
    // small convenience map; unmapped codes render as the bare code (e.g. SEK).
    // Display-only — never touches any value or calculation.
    _updateCurrency(hass) {
      const code = hass?.states?.[this._dashboardEid]?.attributes?.currency;
      if (!code) return;
      this._currency = code;
      const SYMBOLS = { EUR: '€', USD: '$', GBP: '£', JPY: '¥', CNY: '¥', INR: '₹', RUB: '₽', KRW: '₩', BRL: 'R$', ILS: '₪', TRY: '₺' };
      this._currencySymbol = SYMBOLS[code] || code;
    }

    // Re-render the heavy ECharts (price/generation/power) whenever the
    // coordinator pushes a fresh cycle. `set hass` fires on *every* HA state
    // change, so gate on the dashboard sensor's last_updated (which only
    // advances when the coordinator rewrites it, i.e. once per ~5-min tick or a
    // verify-loop mutation) and debounce, so unrelated state pushes between
    // cycles don't thrash echarts or re-fetch history. Without this the charts
    // render once at boot and then freeze until the page is reloaded.
    _maybeRefreshCharts(hass) {
      if (!window.echarts) return;            // boot not finished loading ECharts
      const ds = hass.states[this._dashboardEid];
      const stamp = ds ? (ds.last_updated || ds.last_changed) : null;
      if (!stamp || stamp === this._lastDashStamp) return;
      this._lastDashStamp = stamp;
      clearTimeout(this._chartRefreshTimer);
      this._chartRefreshTimer = setTimeout(() => { this._render(); }, 400);
    }

    disconnectedCallback() {
      if (this._resizeObs) { this._resizeObs.disconnect(); this._resizeObs = null; }
      if (this._resizeRaf) { cancelAnimationFrame(this._resizeRaf); this._resizeRaf = 0; }
      if (this._chart)   { this._chart.dispose();   this._chart   = null; }
      if (this._g1Chart) { this._g1Chart.dispose(); this._g1Chart = null; }
      if (this._g2Chart) { this._g2Chart.dispose(); this._g2Chart = null; }
      if (this._g3Chart) { this._g3Chart.dispose(); this._g3Chart = null; }
      this._qualityCharts = [];
    }

    // ── Boot ──────────────────────────────────────────────────────────────────

    async _boot() {
      this._buildShell();
      try {
        await loadECharts();
      } catch (e) {
        this._setStatus('⚠ ' + e.message + ' — check network or install ECharts via HACS.');
        return;
      }
      await this._render();
    }

    _buildShell() {
      this.shadowRoot.innerHTML = `
        <style>
          :host {
            display: block;
            padding: 16px;
            box-sizing: border-box;
            background: var(--primary-background-color, #111);
            min-height: 100vh;
          }
          #card {
            background: var(--card-background-color, #1c1c1c);
            border-radius: 12px;
            padding: 20px;
          }
          h2 {
            margin: 0 0 2px;
            font-size: 1.25rem;
            color: var(--primary-text-color, #fff);
          }
          #subtitle {
            margin: 0 0 16px;
            font-size: 0.75rem;
            color: var(--secondary-text-color, #888);
          }
          #battery {
            display: flex;
            align-items: baseline;
            gap: 8px;
            margin: 0 0 12px;
            font-size: 0.95rem;
            color: var(--primary-text-color, #fff);
          }
          #battery .label { color: var(--secondary-text-color, #888); }
          #battery .state { font-weight: 600; }
          #battery .state.charging    { color: #4caf50; }
          #battery .state.discharging { color: #ff7043; }
          #battery .state.idle        { color: #9e9e9e; }
          #battery .soc { font-weight: 600; }
          #battery .capacity {
            font-size: 0.75rem;
            color: var(--secondary-text-color, #888);
          }
          #battery .capacity-set {
            font-size: 0.7rem;
            color: var(--secondary-text-color, #888);
            opacity: 0.7;
          }
          #generation {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 6px 10px;
            margin: 0 0 12px;
            font-size: 0.85rem;
            color: var(--primary-text-color, #fff);
          }
          #generation .day {
            display: inline-flex;
            align-items: baseline;
            gap: 4px;
            padding: 3px 8px;
            border-radius: 4px;
            background: rgba(255, 255, 255, 0.04);
            border-left: 3px solid rgba(66, 165, 245, 0.40);
            line-height: 1.3;
          }
          #generation .day.today { border-left-color: #42a5f5; background: rgba(66, 165, 245, 0.10); }
          #generation .day.past  { border-left-color: rgba(158, 158, 158, 0.60); }
          #generation .day .name { color: var(--secondary-text-color, #888); font-weight: 600; }
          #generation .day .pred { font-weight: 600; }
          #generation .day .actual { color: #66bb6a; font-weight: 600; }
          #generation .day .remain { color: #ffb300; font-size: 0.75rem; }
          #generation .day .sep  { color: var(--secondary-text-color, #444); }
          #generation .day .unit { color: var(--secondary-text-color, #888); font-size: 0.75rem; }
          #status {
            padding: 40px;
            text-align: center;
            color: var(--secondary-text-color, #888);
          }
          #chart { width: 100%; }
          #accuracy { margin-top: 24px; }
          .accuracy-title {
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--secondary-text-color, #888);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin: 16px 0 4px;
          }
          .accuracy-subtitle {
            font-size: 0.75rem;
            color: var(--secondary-text-color, #666);
            margin: 0 0 4px;
          }
          .accuracy-chart { width: 100%; }
          .bill-title {
            font-size: 0.85rem;
            font-weight: 600;
            color: var(--secondary-text-color, #888);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin: 16px 0 4px;
          }
          .bill-summary {
            display: flex;
            flex-wrap: wrap;
            align-items: baseline;
            gap: 6px 8px;
            margin: 0 0 8px;
            font-size: 0.85rem;
            color: var(--primary-text-color, #fff);
          }
          .bill-summary .bill-label { color: var(--secondary-text-color, #888); }
          .bill-summary .bill-value { font-weight: 600; }
          .bill-summary .bill-value.revenue { color: #66bb6a; }
          .bill-summary .bill-sep { color: var(--secondary-text-color, #444); }
          .header-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
          }
          #schedule-toggle {
            background: rgba(255, 255, 255, 0.06);
            color: var(--primary-text-color, #fff);
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 6px;
            padding: 6px 12px;
            font-size: 0.8rem;
            cursor: pointer;
            font-family: inherit;
            display: inline-flex;
            align-items: center;
            gap: 6px;
          }
          #schedule-toggle:hover { background: rgba(255, 255, 255, 0.10); }
          #schedule-toggle.open { background: rgba(66, 165, 245, 0.18); border-color: rgba(66, 165, 245, 0.50); }
          #schedule-panel {
            display: none;
            margin: 0 0 16px;
            padding: 14px 16px;
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 8px;
          }
          #schedule-panel.open { display: block; }
          .sched-section-title {
            font-size: 0.75rem;
            font-weight: 600;
            color: var(--secondary-text-color, #888);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin: 0 0 8px;
          }
          .sched-section + .sched-section { margin-top: 14px; }
          .sched-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 8px 16px;
          }
          .sched-row {
            display: flex;
            align-items: center;
            justify-content: flex-start;
            gap: 10px;
            padding: 6px 0;
            font-size: 0.85rem;
            color: var(--primary-text-color, #fff);
          }
          .sched-row.sched-row-stacked { flex-wrap: wrap; }
          .sched-row.sched-row-stacked .sched-readout { flex: 1 0 100%; }
          .sched-row .sched-label {
            color: var(--secondary-text-color, #bbb);
            flex: 1 1 auto;
            min-width: 0;
          }
          .sched-row .sched-missing {
            color: #ef5350;
            font-size: 0.75rem;
          }
          .sched-toggle {
            position: relative;
            display: inline-block;
            width: 36px;
            height: 20px;
            flex: 0 0 auto;
          }
          .sched-toggle input { opacity: 0; width: 0; height: 0; }
          .sched-toggle .slider {
            position: absolute;
            cursor: pointer;
            inset: 0;
            background-color: #555;
            border-radius: 20px;
            transition: background-color 0.15s;
          }
          .sched-toggle .slider:before {
            position: absolute;
            content: "";
            height: 14px;
            width: 14px;
            left: 3px;
            top: 3px;
            background-color: #fff;
            border-radius: 50%;
            transition: transform 0.15s;
          }
          .sched-toggle input:checked + .slider { background-color: #42a5f5; }
          .sched-toggle input:checked + .slider:before { transform: translateX(16px); }
          .sched-toggle input:disabled + .slider { opacity: 0.4; cursor: not-allowed; }
          .sched-num {
            display: flex;
            align-items: center;
            gap: 6px;
            flex: 0 0 auto;
          }
          .sched-num input {
            width: 78px;
            padding: 4px 6px;
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 4px;
            color: var(--primary-text-color, #fff);
            font-family: inherit;
            font-size: 0.85rem;
            text-align: right;
          }
          .sched-num input:focus { outline: 1px solid #42a5f5; outline-offset: -1px; }
          .sched-num input:disabled { opacity: 0.4; cursor: not-allowed; }
          .sched-num .sched-unit {
            color: var(--secondary-text-color, #888);
            font-size: 0.75rem;
          }
          .sched-select {
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
            flex: 1 1 auto;
            min-width: 0;
          }
          .sched-select button {
            background: rgba(255, 255, 255, 0.06);
            color: var(--primary-text-color, #fff);
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 4px;
            padding: 3px 8px;
            font-family: inherit;
            font-size: 0.78rem;
            cursor: pointer;
            transition: background 0.12s, border-color 0.12s, color 0.12s;
          }
          .sched-select button:hover { background: rgba(255, 255, 255, 0.10); }
          .sched-select button.selected {
            background: rgba(66, 165, 245, 0.22);
            border-color: rgba(66, 165, 245, 0.55);
            color: #fff;
          }
          .sched-select button:disabled {
            opacity: 0.4;
            cursor: not-allowed;
            background: rgba(255, 255, 255, 0.04);
          }
          .sched-readout {
            margin-top: 4px;
            font-size: 0.78rem;
            color: var(--secondary-text-color, #888);
            word-break: break-all;
            font-family: ui-monospace, "SF Mono", Menlo, monospace;
          }
          .sched-readout .sched-readout-label {
            color: var(--secondary-text-color, #666);
            margin-right: 4px;
          }
          /* ── Control block: override | intended · observed | policy | tuning ──
             A wrapping flex row: the override buttons and the intended/observed
             register grid stay tight on the left; policy + tuning columns fill
             the remaining width and wrap below on narrow layouts. */
          .mode-override-row { display: block; padding: 4px 0; }
          .mode-override-row .mo-grid {
            display: flex;
            flex-wrap: wrap;
            gap: 12px 22px;
            align-items: flex-start;
          }
          .mo-cap {
            font-size: 0.62rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: var(--secondary-text-color, #888);
          }
          .mo-override-col { display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 0 0 auto; }
          /* Override options stack vertically — compact, and roughly the same
             height as the register grid instead of wrapping full-width. */
          .mo-override-col .sched-select.mo-buttons {
            flex-direction: column;
            align-items: stretch;
            gap: 3px;
          }
          .mo-override-col .sched-select.mo-buttons button { text-align: left; }
          /* sunSale sentinel's "[<mode>]" suffix — the mode it's dispatching. */
          .mo-buttons .mo-sub {
            font-size: 0.72rem;
            opacity: 0.7;
            margin-left: 4px;
            font-variant-numeric: tabular-nums;
          }

          /* Policy / tuning columns — ride alongside the readout, fill the rest
             of the row and wrap when there isn't room. */
          .ctl-col { display: flex; flex-direction: column; gap: 2px; min-width: 0; flex: 1 1 190px; }
          .ctl-col .ctl-list { display: flex; flex-direction: column; }
          .ctl-col .ctl-list .sched-row { padding: 3px 0; gap: 8px; font-size: 0.8rem; }
          .ctl-col .ctl-list .sched-label { font-size: 0.78rem; }
          /* Per-knob info affordance (hover / focus for the description). */
          .sched-info {
            flex: 0 0 auto;
            color: var(--secondary-text-color, #888);
            font-size: 0.8rem;
            cursor: help;
            opacity: 0.65;
            transition: opacity 0.12s, color 0.12s;
          }
          .sched-info:hover, .sched-info:focus { opacity: 1; color: #42a5f5; outline: none; }

          /* Readout half: one grid so intended/observed values align under
             their headers. Reset the generic .sched-readout mono/break styling.
             Stays content-tight (flex 0 1 auto) so the columns read compact. */
          .sched-readout-mode {
            margin-top: 0;
            word-break: normal;
            font-family: inherit;
            min-width: 0;
            flex: 0 1 auto;
          }
          .mo-table {
            display: grid;
            grid-template-columns: auto auto auto;
            gap: 2px 8px;
            align-items: baseline;
            font-family: ui-monospace, "SF Mono", Menlo, monospace;
            font-size: 0.72rem;
          }
          .mo-th { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
          .mo-table .mo-cap { font-size: 0.6rem; }
          .mo-mode {
            font-size: 0.82rem;
            font-weight: 600;
            color: var(--primary-text-color, #eee);
            font-family: inherit;
          }
          .mo-mode-row { display: flex; align-items: center; gap: 5px; min-width: 0; }
          .reg-label { color: var(--secondary-text-color, #888); white-space: nowrap; }
          .reg-intended, .reg-observed {
            text-align: right;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
          }
          .reg-intended { color: var(--secondary-text-color, #aaa); }
          .reg-observed { font-weight: 600; border-radius: 3px; padding: 0 3px; }
          .reg-observed.reg-ok   { color: #66bb6a; }
          .reg-observed.reg-wait { color: #ffa726; }
          .reg-observed.reg-bad  { color: #ef5350; background: rgba(239, 83, 80, 0.12); }
          .reg-observed.reg-na   { color: var(--secondary-text-color, #888); font-weight: 500; }

          /* Verify badge — reused in the Observed column header. */
          .sched-readout-mode .verify-badge {
            padding: 0 5px;
            border-radius: 10px;
            font-size: 0.72rem;
            font-weight: 600;
            line-height: 1.4;
          }
          .sched-readout-mode .verify-badge.ok {
            background: rgba(102, 187, 106, 0.18);
            color: #66bb6a;
          }
          .sched-readout-mode .verify-badge.pending {
            background: rgba(255, 167, 38, 0.18);
            color: #ffa726;
          }
          .sched-readout-mode .verify-badge.mismatch {
            background: rgba(239, 83, 80, 0.22);
            color: #ef5350;
          }
          /* Pending button: pulsing orange outline while verify is in flight. */
          .sched-select button.selected.pending {
            border-color: rgba(255, 167, 38, 0.75);
            animation: sched-pulse 1.4s ease-in-out infinite;
          }
          @keyframes sched-pulse {
            0%, 100% { box-shadow: 0 0 0 1px rgba(255, 167, 38, 0.35); }
            50%      { box-shadow: 0 0 0 4px rgba(255, 167, 38, 0.10); }
          }
          /* Mismatch button: red outline, persistent (not animated). */
          .sched-select button.selected.mismatch {
            border-color: rgba(239, 83, 80, 0.75);
            background: rgba(239, 83, 80, 0.18);
          }

          /* ── Battery / forecast info + flow diagram row ────────────────────
             The flow diagram floats to the right of the battery + forecast
             info (below the Schedule button) when there's room, and wraps
             below on narrow layouts. info-left flex-grows so the diagram is
             pushed to the right edge while they share a line. */
          #info-row {
            display: flex;
            flex-wrap: wrap;
            align-items: flex-start;
            gap: 4px 24px;
          }
          #info-left { flex: 1 1 340px; min-width: 0; }
          #flow-section { flex: 0 0 auto; padding-top: 2px; }

          /* ── Live energy-flow readout (mini hub diagram) ───────────────────
             Two lines: the DC sources (solar + battery, each with an inline
             direction arrow) sit on the upper line directly above the losses
             node; grid feeds in from the left and home draws from the right on
             the lower line. Each leg's arrow bobs along its axis toward / away
             from the hub. Fed by dashAttrs.live_flows (signs: battery
             +charging, grid +import — see _build_live_flows). */
          #flow-diagram { width: auto; }
          .flow-grid {
            display: grid;
            grid-template-columns: auto auto auto auto auto auto;
            align-items: center;
            justify-items: center;
            column-gap: 5px;
            row-gap: 2px;
          }
          .fg-cell {
            display: inline-flex;
            align-items: baseline;
            gap: 3px;
            white-space: nowrap;
          }
          /* Two lines: solar + battery (each carrying an inline direction
             arrow) sit on row 1 directly above the losses node, which spans
             their two columns (3–4) on row 2. Grid flanks left (cols 1–2),
             home right (cols 5–6). */
          .fg-solar { grid-area: 1 / 3; }
          .fg-batt  { grid-area: 1 / 4; }
          .fg-grid  { grid-area: 2 / 1; }
          .fg-g2l   { grid-area: 2 / 2; }
          .fg-loss  { grid-area: 2 / 3 / 3 / 5; }
          .fg-l2h   { grid-area: 2 / 5; }
          .fg-home  { grid-area: 2 / 6; }
          .flow-ico { font-size: 12px; line-height: 1; }
          .flow-val {
            font-size: 14px;
            font-weight: 700;
            color: var(--primary-text-color, #fff);
            font-variant-numeric: tabular-nums;
            line-height: 1.2;
          }
          .flow-val.solar    { color: #fbc02d; }
          .flow-val.grid-in  { color: #ef5350; }
          .flow-val.grid-out { color: #66bb6a; }
          .flow-val.batt-in  { color: #26a69a; }
          .flow-val.batt-out { color: #42a5f5; }
          .flow-val.loss     { color: #ab47bc; }
          .flow-val.home     { color: #26c6da; }
          .flow-val.idle     { color: var(--secondary-text-color, #9e9e9e); }
          .flow-unit {
            font-size: 8px;
            font-weight: 600;
            color: var(--secondary-text-color, #8a8a8a);
            margin-left: 1.5px;
          }
          /* Direction arrow: points toward / away from the central losses hub
             (↓ solar into hub · → / ← grid import / export · ↑ / ↓ battery
             charge / discharge · → hub out to home). Each bobs along its own
             axis to read as live; speed scales with the leg's magnitude. */
          .flow-arr {
            display: inline-block;
            font-size: 12px;
            font-weight: 700;
            line-height: 1;
          }
          .flow-arr.solar    { color: #fbc02d; }
          .flow-arr.grid-in  { color: #ef5350; }
          .flow-arr.grid-out { color: #66bb6a; }
          .flow-arr.batt-in  { color: #26a69a; }
          .flow-arr.batt-out { color: #42a5f5; }
          .flow-arr.home     { color: #26c6da; }
          .flow-arr.a-up    { animation: fa-up    1.1s ease-in-out infinite; }
          .flow-arr.a-down  { animation: fa-down  1.1s ease-in-out infinite; }
          .flow-arr.a-left  { animation: fa-left  1.1s ease-in-out infinite; }
          .flow-arr.a-right { animation: fa-right 1.1s ease-in-out infinite; }
          @keyframes fa-up    { 0%, 100% { transform: translateY( 2px); opacity: 0.5; } 50% { transform: translateY(-2px); opacity: 1; } }
          @keyframes fa-down  { 0%, 100% { transform: translateY(-2px); opacity: 0.5; } 50% { transform: translateY( 2px); opacity: 1; } }
          @keyframes fa-left  { 0%, 100% { transform: translateX( 2px); opacity: 0.5; } 50% { transform: translateX(-2px); opacity: 1; } }
          @keyframes fa-right { 0%, 100% { transform: translateX(-2px); opacity: 0.5; } 50% { transform: translateX( 2px); opacity: 1; } }
          @media (prefers-reduced-motion: reduce) {
            .flow-arr.a-up, .flow-arr.a-down, .flow-arr.a-left, .flow-arr.a-right {
              animation: none; opacity: 1;
            }
          }
          .flow-sub {
            font-size: 8px;
            font-weight: 600;
            color: var(--secondary-text-color, #8a8a8a);
            font-variant-numeric: tabular-nums;
          }
          /* Reclaim horizontal space on phones — the host + card padding eats
             ~20% of a 360px viewport, squeezing the charts. */
          @media (max-width: 600px) {
            :host { padding: 8px; }
            #card { padding: 12px; }
          }
        </style>
        <div id="card">
          <div class="header-row">
            <div>
              <h2>☀ Sun Sale</h2>
              <div id="subtitle">Buy &amp; Sell prices · Solar — 72 h window</div>
            </div>
            <button id="schedule-toggle" type="button" title="Edit control parameters">
              <span>⚙</span><span>Control</span>
            </button>
          </div>
          <div id="schedule-panel"></div>
          <div id="info-row">
            <div id="info-left">
              <div id="battery"></div>
              <div id="generation"></div>
            </div>
            <div id="flow-section">
              <div id="flow-diagram"></div>
            </div>
          </div>
          <div id="status">Loading…</div>
          <div id="chart"></div>
          <div id="bill"></div>
          <div id="accuracy"></div>
        </div>
      `;

      const toggleBtn = this.shadowRoot.querySelector('#schedule-toggle');
      const panel     = this.shadowRoot.querySelector('#schedule-panel');
      toggleBtn.addEventListener('click', () => {
        const open = panel.classList.toggle('open');
        toggleBtn.classList.toggle('open', open);
        if (open) this._renderScheduleDrawer();
      });
    }

    _renderBatteryRow(dashAttrs) {
      const el = this.shadowRoot.querySelector('#battery');
      if (!el) return;
      if (!dashAttrs) { el.innerHTML = ''; return; }

      const state    = dashAttrs.battery_state || 'idle';
      const soc      = dashAttrs.battery_soc_pct;
      const charged  = dashAttrs.battery_remaining_kwh;
      const capacity = dashAttrs.battery_capacity_kwh;      // learned (estimated) usable capacity
      const setCap   = dashAttrs.battery_set_capacity_kwh;  // configured nameplate ("set")

      const stateLabel = state.charAt(0).toUpperCase() + state.slice(1);
      const socTxt = (typeof soc === 'number') ? soc.toFixed(1) + '%' : '—';
      const capTxt = (typeof charged === 'number' && typeof capacity === 'number')
        ? `${charged.toFixed(2)} / ${capacity.toFixed(2)} kWh`
        : '';
      const setTxt = (typeof setCap === 'number') ? `set ${setCap.toFixed(1)} kWh` : '';

      el.innerHTML = `
        <span class="label">Battery:</span>
        <span class="state ${state}">${stateLabel}</span>
        <span class="soc">${socTxt}</span>
        ${capTxt ? `<span class="capacity" title="Remaining / learned (estimated) usable capacity">${capTxt}</span>` : ''}
        ${setTxt ? `<span class="capacity-set" title="Configured nameplate (set) capacity">${setTxt}</span>` : ''}
      `;
    }

    _renderGenerationRow(dashAttrs) {
      const el = this.shadowRoot.querySelector('#generation');
      if (!el) return;
      if (!dashAttrs) { el.innerHTML = ''; return; }

      const daily = dashAttrs.forecast_daily_kwh;
      if (!daily) { el.innerHTML = ''; return; }

      const fmt = (v) => (typeof v === 'number' ? v.toFixed(2) : '—');

      const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
      const OFFSETS   = [-1, 0, 1, 2, 3, 4, 5, 6];
      const KEYS      = ['yesterday', 'today', 'tomorrow', 'd2', 'd3', 'd4', 'd5', 'd6'];

      const dayLabel = (off) => {
        if (off === -1) return 'Yday';
        if (off === 0)  return 'Today';
        if (off === 1)  return 'Tmrw';
        const d = new Date();
        d.setDate(d.getDate() + off);
        return DAY_NAMES[d.getDay()];
      };

      const parts = OFFSETS.map((off, i) => {
        const pred = daily[KEYS[i]];
        const predTxt = fmt(pred);
        const cls = off < 0 ? 'past' : (off === 0 ? 'today' : '');

        let inner = `<span class="name">${dayLabel(off)}</span>`;
        if (off === -1) {
          const actual = dashAttrs.actual_yesterday_kwh;
          inner += `<span class="pred">${predTxt}</span>`
                +  `<span class="sep">/</span>`
                +  `<span class="actual">${fmt(actual)}</span>`
                +  `<span class="unit">kWh</span>`;
        } else if (off === 0) {
          const actual = dashAttrs.actual_today_kwh;
          inner += `<span class="pred">${predTxt}</span>`
                +  `<span class="sep">/</span>`
                +  `<span class="actual">${fmt(actual)}</span>`
                +  `<span class="unit">kWh</span>`;
        } else {
          inner += `<span class="pred">${predTxt}</span>`
                +  `<span class="unit">kWh</span>`;
        }
        return `<span class="day ${cls}">${inner}</span>`;
      });

      el.innerHTML = parts.join('');
    }

    // ── Live energy-flow readout ─────────────────────────────────────────────
    // Mini two-line hub diagram of "where is power flowing right now". The
    // central node (⚙) carries the inverter losses (shown in watts). Solar +
    // battery are the DC sources on the upper line, their arrows pointing in
    // toward the hub between them (solar's trails, battery's leads); grid feeds
    // in from the left and home draws from the right on the lower line:
    //
    //   ☀ Solar ↓        ↑/↓ 🔋 Batt 78%
    //   ⚡ Grid →/←  ⚙ losses(W)  → 🏠 Home
    //
    // Each leg shows a direction arrow pointing toward / away from the hub,
    // bobbing along its own axis at a magnitude-scaled speed. Fed by
    // dashAttrs.live_flows (signs: battery +charging, grid +import — see
    // DashboardSensor._build_live_flows).

    // Compute the live flow legs client-side from the raw inverter sensors named
    // in dashAttrs.live_flow_sources, so the diagram tracks the inverter in real
    // time (every HA state push) instead of the ~5-min coordinator cadence — and
    // sidesteps the derived-sample freshness gate that starved live_flows. Each
    // leg spec is {entity_id, scale, sign, clamp_min?}; the server pre-folds the
    // unit→kW scale and sunSale sign convention (SunSaleCoordinator.
    // _build_live_flow_sources), so the panel just evaluates value*scale*sign and
    // the same home/losses formulas as _build_live_flows. Falls back to the
    // server-computed dashAttrs.live_flows when no sources are published (older
    // backend) or no leg resolves (all source sensors missing/unavailable).
    _computeLiveFlows(dashAttrs) {
      const src    = dashAttrs?.live_flow_sources;
      const states = this._hass?.states;
      if (!src || !states) return dashAttrs?.live_flows || null;

      let resolvedAny = false;
      const leg = (spec) => {
        if (!spec || !spec.entity_id) return null;
        const st = states[spec.entity_id];
        if (!st) return null;
        const raw = st.state;
        if (raw == null || raw === 'unavailable' || raw === 'unknown' || raw === '') return null;
        const v = parseFloat(raw);
        if (!Number.isFinite(v)) return null;
        resolvedAny = true;
        let kw = v * (Number(spec.scale) || 0) * (Number(spec.sign) || 0);
        if (typeof spec.clamp_min === 'number') kw = Math.max(spec.clamp_min, kw);
        return kw;
      };

      // A leg that is unmapped or momentarily unavailable contributes 0 (an
      // install with no backup port, say), rather than dropping the whole
      // diagram the way the server's all-or-nothing derived composer does.
      const solar = leg(src.solar)   || 0;
      const batt  = leg(src.battery) || 0;
      const grid  = leg(src.grid)    || 0;
      const acp   = leg(src.ac_port) || 0;
      const bkup  = leg(src.backup)  || 0;

      // Nothing resolved (every source sensor gone) → fall back to the server's
      // last cycle value instead of a misleading all-zero diagram.
      if (!resolvedAny) return dashAttrs?.live_flows || null;

      const soc = (typeof dashAttrs.battery_soc_pct === 'number') ? dashAttrs.battery_soc_pct : null;
      return {
        ts:              Date.now(),
        solar_kw:        solar,
        battery_kw:      batt,
        grid_kw:         grid,
        home_kw:         Math.max(0, bkup + acp + grid),
        losses_kw:       Math.max(0, solar - batt - acp - bkup),
        battery_soc_pct: soc,
      };
    }

    _renderFlowDiagram(dashAttrs) {
      const section = this.shadowRoot?.querySelector('#flow-section');
      const el      = this.shadowRoot?.querySelector('#flow-diagram');
      if (!section || !el) return;

      const f = this._computeLiveFlows(dashAttrs);
      if (!f) { section.style.display = 'none'; el.innerHTML = ''; return; }
      section.style.display = '';

      const EPS     = 0.02;
      const solar   = Math.max(0, Number(f.solar_kw) || 0);
      const battery = Number(f.battery_kw) || 0;   // + charging / − discharging
      const grid    = Number(f.grid_kw)    || 0;   // + import   / − export
      const home    = Math.max(0, Number(f.home_kw)   || 0);
      const losses  = Math.max(0, Number(f.losses_kw) || 0);
      const soc     = (typeof f.battery_soc_pct === 'number') ? f.battery_soc_pct : null;

      const kw  = (v) => (Math.abs(v) < 0.005 ? '0.00' : Math.abs(v).toFixed(2));
      // Losses are small, so the hub node reads in whole watts (e.g. 120 W).
      const watt = (v) => `${Math.round(Math.abs(v) * 1000)}`;
      // Faster bob for bigger flows (clamped to a comfortable band).
      const dur = (m) => Math.max(0.5, Math.min(2.0, 1.1 / Math.max(0.12, Math.abs(m)))).toFixed(2);

      // node(): one "icon value[unit] (arrow) (sub)" cluster. The value colour
      // follows the established palette via valCls; idle (< 20 W) renders grey.
      // unit defaults to kW; arrHtml is an optional trailing inline arrow.
      const node = (ico, valTxt, valCls, sub, arrHtml, unit) => {
        const subHtml = sub ? `<span class="flow-sub">${sub}</span>` : '';
        return `<span class="flow-ico">${ico}</span>`
             + `<span class="flow-val ${valCls}">${valTxt}<span class="flow-unit">${unit || 'kW'}</span></span>`
             + (arrHtml || '')
             + subHtml;
      };
      // arr(): a single directional arrow bobbing along its axis. dir ∈
      // {up,down,left,right} sets the glyph's animation; cls carries the leg
      // colour; mag scales the bob speed. Empty string when the leg is idle.
      const arr = (glyph, cls, dir, mag) =>
        `<span class="flow-arr ${cls} a-${dir}" style="animation-duration:${dur(mag)}s">${glyph}</span>`;

      // Per-leg direction relative to the central losses hub. Solar always
      // feeds in (↓); grid is →import / ←export; battery (drawn above the hub)
      // is ↑charge / ↓discharge; the hub always drives home (→) when home draws.
      const solarArr = solar    >  EPS ? arr('↓', 'solar',    'down',  solar)   : '';
      const gridArr  = grid     >  EPS ? arr('→', 'grid-in',  'right', grid)
                     : grid     < -EPS ? arr('←', 'grid-out', 'left',  grid)    : '';
      const homeArr  = home     >  EPS ? arr('→', 'home',     'right', home)    : '';
      const battArr  = battery  >  EPS ? arr('↑', 'batt-in',  'up',    battery)
                     : battery  < -EPS ? arr('↓', 'batt-out', 'down',  battery) : '';

      const solarCls = solar   >  EPS ? 'solar' : 'idle';
      const gridCls  = grid    >  EPS ? 'grid-in' : (grid    < -EPS ? 'grid-out' : 'idle');
      const battCls  = battery >  EPS ? 'batt-in' : (battery < -EPS ? 'batt-out' : 'idle');
      const homeCls  = home    >  EPS ? 'home' : 'idle';
      const lossCls  = losses  >  EPS ? 'loss' : 'idle';
      const battSub  = (soc != null) ? `${soc.toFixed(0)}%` : '';

      el.innerHTML = `
        <div class="flow-grid" role="img"
             aria-label="Live energy flow: solar and battery feed the inverter losses from above; grid feeds from the left, home draws from the right">
          <span class="fg-cell fg-solar">${node('☀', kw(solar), solarCls, '', solarArr)}</span>
          <span class="fg-cell fg-batt">${battArr}${node('🔋', kw(battery), battCls, battSub)}</span>
          <span class="fg-cell fg-grid">${node('⚡', kw(grid), gridCls)}</span>
          <span class="fg-cell fg-g2l">${gridArr}</span>
          <span class="fg-cell fg-loss">${node('⚙', watt(losses), lossCls, '', '', 'W')}</span>
          <span class="fg-cell fg-l2h">${homeArr}</span>
          <span class="fg-cell fg-home">${node('🏠', kw(home), homeCls)}</span>
        </div>`;
    }

    // ── Schedule parameters drawer ────────────────────────────────────────────
    // Inline editor for sunSale's schedule policy switches + numeric knobs.
    // Entities are resolved by suffix-match against the `sunsale_` prefix so
    // the panel works even if HA renames the entity_id slug (e.g. `α` in the
    // Profitability-Tilt name is unidecoded inconsistently across HA versions).

    _SCHEDULE_SWITCHES = [
      { key: 'automation',             label: 'Automation enabled',     match: ['automation'],
        info: 'When off, sunSale still computes schedules but sends no commands to the inverter (observer-only).' },
      { key: 'use_standby',            label: 'Use standby (night)',    match: ['use_standby'],
        info: 'When on, the scheduler may pick StandBy (battery idle) during no-generation windows.' },
      { key: 'allow_grid_charging',    label: 'Allow grid charging',    match: ['allow_grid_charging'],
        info: 'When on, the scheduler may force grid-charge the battery at low prices.' },
      { key: 'allow_feed_in',          label: 'Allow feed-in',          match: ['allow_feed_in'],
        info: 'When on, the scheduler may prioritise feed-in to the grid during surplus-solar slots.' },
      { key: 'allow_discharge_to_grid',label: 'Allow discharge to grid',match: ['allow_discharge_to_grid'],
        info: 'When on, the scheduler may pick the explicit Discharge-to-grid mode at high sell prices.' },
    ];

    _SCHEDULE_NUMBERS = [
      { key: 'mode_change_penalty',    label: 'Mode-change penalty',    match: ['mode_change_penalty'], unit: '',
        info: 'Per-kWh penalty deducted whenever the planner switches mode between slots. Higher → fewer, steadier mode changes; lower → the planner chases smaller price gaps.' },
      { key: 'profitability_tilt',     label: 'Profitability tilt α',   match: ['profitability_tilt'],  unit: '',
        info: 'Strength of the profitability-score bias on the value of battery charge left at the end of the planning horizon. Higher → hold charge harder on days that look profitable to sell.' },
      { key: 'terminal_value_discount',label: 'Terminal-value discount',match: ['terminal_value_discount'], unit: '',
        info: 'Multiplier on the in-horizon median sell price when valuing leftover end-of-horizon battery charge. Lower → value stored energy less, so the planner discharges more freely before the window ends.' },
      { key: 'max_discharge_to_grid',  label: 'Max discharge to grid',  match: ['max_discharge_to_grid'], unit: 'kW',
        info: 'AC power cap (kW) for Discharge-to-grid mode. Below the hardware max it limits how aggressively the planner schedules grid export.' },
    ];

    _SCHEDULE_SELECTS = [
      { key: 'mode_override', label: 'Mode override', match: ['mode_override'],
        // Sensor that shows the observed inverter state next to the override.
        readout_sensor_match: ['observed_inverter_mode'],
        readout_label: 'Observed',
        // Display labels for the select options. The raw entity option strings
        // (e.g. "sunsale", "self_use") are snake_case for HA compatibility;
        // these prettier labels are used only when rendering buttons.
        option_labels: {
          sunsale:     'sunSale',
          self_use:    'Self-use',
          no_export:   'No export',
          stand_by:    'Standby',
          grid_charge: 'Grid charge',
          discharge:   'Discharge',
          feed_in:     'Feed-in',
        } },
    ];

    // Compact column labels for the register comparison grid, keyed by the
    // register ``name`` exposed in the observed-mode sensor's ``register_panel``
    // attribute. Falls back to the sensor's full label, then the raw name.
    _REG_SHORT_LABELS = {
      reg_43110:      'Reg 43110',
      charge_a:       'Charge A',
      discharge_a:    'Dischg A',
      export_limit_w: 'Export W',
      rc_enable:      'RC sel',
      rc_setpoint_w:  'RC W',
    };

    _findEntityId(domain, matchAll) {
      if (!this._hass?.states) return null;
      const prefix = `${domain}.sunsale_`;
      for (const eid of Object.keys(this._hass.states)) {
        if (!eid.startsWith(prefix)) continue;
        // When scoped to a config entry, ignore entities of sibling entries.
        if (this._entryEntityIds && !this._entryEntityIds.has(eid)) continue;
        if (matchAll.every(m => eid.includes(m))) return eid;
      }
      return null;
    }

    // ── Per-entry entity scoping ──────────────────────────────────────────────
    // Each sunSale config entry owns a device (identifiers {(sun_sale, entry_id)})
    // and its entities' unique_ids are `<entry_id>_<key>`. With multiple entries
    // HA suffixes colliding entity_id slugs (`..._2`), so the panel must resolve
    // entities for *its* entry, not by bare slug. We scope via the device: find
    // the device(s) for panel.config.entry_id, then restrict to their entities.
    // When the entry id or device registry is unavailable (single install, older
    // HA, panel.config not yet set) scoping is disabled and the legacy bare-slug
    // resolution applies — so the common single-entry case is never affected.
    _scopeEntitiesToEntry(hass) {
      const entryId = this.panel?.config?.entry_id;
      let deviceIds = null;
      if (entryId && hass?.devices) {
        deviceIds = new Set();
        for (const id of Object.keys(hass.devices)) {
          const d = hass.devices[id] || {};
          const inEntry =
            (d.config_entries || []).includes(entryId) ||
            (d.identifiers || []).some(x => x && x[0] === 'sun_sale' && x[1] === entryId);
          if (inEntry) deviceIds.add(id);
        }
        if (!deviceIds.size) deviceIds = null;   // device not registered yet
      }

      const entities = hass?.entities || {};
      const states = hass?.states || {};
      const ids = new Set();
      const byKey = {};
      for (const eid of Object.keys(states)) {
        if (!eid.includes('.sunsale_')) continue;
        if (deviceIds) {
          const reg = entities[eid];
          if (!reg || !reg.device_id || !deviceIds.has(reg.device_id)) continue;
        }
        ids.add(eid);
        const slug = eid.slice(eid.indexOf('.') + 1);     // sunsale_dashboard[_2]
        // Strip the `sunsale_` prefix and any trailing `_<n>` collision suffix.
        const key = slug.replace(/^sunsale_/, '').replace(/_\d+$/, '');
        if (!(key in byKey)) byKey[key] = eid;            // first wins; unique per entry
      }

      this._entryEntityIds = deviceIds ? ids : null;
      this._eidByKey = byKey;
      this._dashboardEid   = this._sid('dashboard',         DASHBOARD_ENTITY);
      this._monthlyBillEid = this._sid('monthly_bill',      MONTHLY_BILL_ENTITY);
      this._pricingEid     = this._sid('pricing',           PRICING_ENTITY);
      this._buyPriceEid    = this._sid('current_buy_price', BUY_PRICE_ENTITY);
      this._sellPriceEid   = this._sid('current_sell_price', SELL_PRICE_ENTITY);
    }

    // Resolve a logical sensor key (`sunsale_<key>`) to this entry's entity_id,
    // falling back to the bare single-install id when scoping is unavailable.
    _sid(key, fallback) {
      return (this._eidByKey && this._eidByKey[key]) || fallback;
    }

    _renderScheduleDrawer() {
      const panel = this.shadowRoot.querySelector('#schedule-panel');
      if (!panel) return;

      const switchRow = (spec) => {
        const eid = this._findEntityId('switch', spec.match);
        if (!eid) {
          return `<div class="sched-row" data-spec="${spec.key}">
            <span class="sched-missing">entity not found</span>
            <span class="sched-label">${spec.label}</span>
          </div>`;
        }
        const st = this._hass.states[eid];
        const on = st?.state === 'on';
        const unavailable = !st || st.state === 'unavailable';
        const tip = spec.info ? `${spec.info}\n(${eid})` : eid;
        return `<div class="sched-row" data-spec="${spec.key}">
          <label class="sched-toggle">
            <input type="checkbox" data-eid="${eid}" data-kind="switch"
                   ${on ? 'checked' : ''} ${unavailable ? 'disabled' : ''}>
            <span class="slider"></span>
          </label>
          <span class="sched-label" title="${this._esc(tip)}">${spec.label}</span>
        </div>`;
      };

      const numberRow = (spec) => {
        const eid = this._findEntityId('number', spec.match);
        if (!eid) {
          return `<div class="sched-row" data-spec="${spec.key}">
            <span class="sched-missing">entity not found</span>
            <span class="sched-label">${spec.label}</span>
          </div>`;
        }
        const st = this._hass.states[eid];
        const unavailable = !st || st.state === 'unavailable' || st.state === 'unknown';
        const value = unavailable ? '' : st.state;
        const attrs = st?.attributes || {};
        const min   = attrs.min  != null ? attrs.min  : 0;
        const max   = attrs.max  != null ? attrs.max  : 1;
        const step  = attrs.step != null ? attrs.step : 0.01;
        const unit  = spec.unit || attrs.unit_of_measurement || '';
        // Info affordance: hover (or focus) the ⓘ for what the knob controls.
        const info  = spec.info
          ? `<span class="sched-info" tabindex="0"
                   title="${this._esc(spec.info)}"
                   aria-label="${this._esc(spec.label + ': ' + spec.info)}">ⓘ</span>`
          : '';
        const tip = spec.info ? `${spec.info}\n(${eid})` : eid;
        return `<div class="sched-row" data-spec="${spec.key}">
          <span class="sched-num">
            <input type="number" data-eid="${eid}" data-kind="number"
                   min="${min}" max="${max}" step="${step}"
                   value="${value}" ${unavailable ? 'disabled' : ''}>
            ${unit ? `<span class="sched-unit">${unit}</span>` : ''}
          </span>
          <span class="sched-label" title="${this._esc(tip)}">${spec.label}</span>
          ${info}
        </div>`;
      };

      const selectRow = (spec) => {
        const eid = this._findEntityId('select', spec.match);
        if (!eid) {
          return `<div class="sched-row sched-row-stacked" data-spec="${spec.key}">
            <span class="sched-missing">entity not found</span>
            <span class="sched-label">${spec.label}</span>
          </div>`;
        }
        const st = this._hass.states[eid];
        const options = Array.isArray(st?.attributes?.options) ? st.attributes.options : [];
        const current = st?.state ?? '';
        const unavailable = !st || st.state === 'unavailable';
        const labels = spec.option_labels || {};
        // Render each option as a button — native <select> dropdowns are
        // unreliable inside HA's shadow-DOM panel surface on some setups.
        const btnsHtml = options.map(opt =>
          `<button type="button" data-eid="${eid}" data-kind="select" data-option="${opt}"
                   class="${opt === current ? 'selected' : ''}"
                   ${unavailable ? 'disabled' : ''}>${labels[opt] || opt}</button>`
        ).join('');
        const readout = this._renderReadout(spec);
        return `<div class="sched-row sched-row-stacked" data-spec="${spec.key}">
          <span class="sched-label" title="${eid}">${spec.label}</span>
          <span class="sched-select">${btnsHtml}</span>
          ${readout}
        </div>`;
      };

      // Single "Control" section: the mode-override row carries Policy and
      // Tuning as extra columns alongside the Override / Intended / Observed
      // columns (see _renderControlRow). Any non-override select still renders
      // through the generic selectRow path.
      const policyHtml = this._SCHEDULE_SWITCHES.map(switchRow).join('');
      const tuningHtml = this._SCHEDULE_NUMBERS.map(numberRow).join('');
      panel.innerHTML = `
        <div class="sched-section">
          <div class="sched-section-title">Control</div>
          ${this._SCHEDULE_SELECTS.map(s =>
            s.key === 'mode_override'
              ? this._renderControlRow(s, policyHtml, tuningHtml)
              : `<div class="sched-grid">${selectRow(s)}</div>`
          ).join('')}
        </div>
      `;

      panel.querySelectorAll('input[data-kind="switch"]').forEach((el) => {
        el.addEventListener('change', () => this._onSwitchChange(el));
      });
      panel.querySelectorAll('input[data-kind="number"]').forEach((el) => {
        el.addEventListener('change', () => this._onNumberChange(el));
      });
      panel.querySelectorAll('button[data-kind="select"]').forEach((el) => {
        el.addEventListener('click', () => this._onSelectClick(el));
      });
    }

    _renderReadout(spec) {
      // Look up the companion sensor that backs a select's "Observed: ..." line.
      // Returns an empty string when no sensor is configured for this spec or
      // when the entity hasn't been published yet. (Mode override has its own
      // multi-column renderer — see _renderControlRow.)
      if (!spec.readout_sensor_match) return '';
      const sid = this._findEntityId('sensor', spec.readout_sensor_match);
      if (!sid) return '';
      const st = this._hass.states[sid];
      const value = st?.state ?? 'unavailable';
      const label = spec.readout_label || 'Observed';
      return `<div class="sched-readout" data-readout-eid="${sid}">
        <span class="sched-readout-label">${label}:</span><span class="sched-readout-value">${value}</span>
      </div>`;
    }

    _renderControlRow(spec, policyHtml, tuningHtml) {
      // Compact multi-column control block (one .sched-row so the in-place sync
      // row count stays consistent — the nested policy/tuning rows are .sched-row
      // too and are counted by _syncScheduleDrawer):
      //   [ Override ] | [ Intended · Observed ] | [ Policy ] | [ Tuning ]
      // The Intended/Observed columns share one aligned register grid (label ·
      // intended · observed) so the operator can always see what each register
      // should be vs what it is — even when the observed bitmask maps to no
      // named mode. Policy (switches) and Tuning (numeric knobs) ride along as
      // the two extra columns; they always render, even if the override select
      // entity is missing, so the operator never loses access to them.
      const eid = this._findEntityId('select', spec.match);
      const st = eid ? this._hass.states[eid] : null;
      const options = Array.isArray(st?.attributes?.options) ? st.attributes.options : [];
      const current = st?.state ?? '';
      const unavailable = !st || st.state === 'unavailable';
      const labels = spec.option_labels || {};
      const sid = this._findEntityId('sensor', spec.readout_sensor_match);
      const modeAttrs = sid ? this._hass.states[sid]?.attributes : null;

      let overrideInner;
      if (!eid) {
        overrideInner = `<span class="sched-missing">entity not found</span>`;
      } else {
        // Buttons (native <select> is unreliable inside HA's shadow-DOM panel).
        const btnsHtml = options.map(opt =>
          `<button type="button" data-eid="${eid}" data-kind="select" data-option="${opt}"
                   class="${opt === current ? 'selected' : ''}"
                   ${unavailable ? 'disabled' : ''}>${this._optionButtonLabel(opt, labels, modeAttrs)}</button>`
        ).join('');
        overrideInner = `<div class="sched-select mo-buttons">${btnsHtml}</div>`;
      }
      const inner = sid ? this._renderModeReadoutInner(spec, sid) : '';
      return `<div class="sched-row mode-override-row" data-spec="${spec.key}">
        <div class="mo-grid">
          <div class="mo-override-col">
            <div class="mo-cap">Override</div>
            ${overrideInner}
          </div>
          <div class="sched-readout sched-readout-mode"
               data-readout-eid="${sid || ''}" data-spec-key="${spec.key}">${inner}</div>
          <div class="ctl-col ctl-policy">
            <div class="mo-cap">Policy</div>
            <div class="ctl-list">${policyHtml}</div>
          </div>
          <div class="ctl-col ctl-tuning">
            <div class="mo-cap">Tuning</div>
            <div class="ctl-list">${tuningHtml}</div>
          </div>
        </div>
      </div>`;
    }

    _optionButtonLabel(opt, labels, modeAttrs) {
      // Display label for an override option button. The sunSale sentinel is
      // annotated with the mode it is currently dispatching — sunSale[<mode>] —
      // resolved from the observed-mode sensor's last_commanded_mode (the
      // inverter-truth target), falling back to last_dispatch_target.
      const base = labels[opt] || opt;
      if (opt !== 'sunsale') return base;
      // Only meaningful while sunSale is the active selection (mode_override is
      // null on the sensor exactly then); under a manual override
      // last_commanded_mode is that override, not what the scheduler resolves.
      if (modeAttrs && modeAttrs.mode_override != null) return base;
      const mode = modeAttrs?.last_commanded_mode || modeAttrs?.last_dispatch_target;
      if (!mode || mode === 'sunsale') return base;
      const pretty = labels[mode] || mode;
      return `${base}<span class="mo-sub">[${this._esc(pretty)}]</span>`;
    }

    _esc(s) {
      // Escape a string for safe interpolation into an HTML attribute / text.
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    _renderModeReadoutInner(spec, sid) {
      // Inner HTML for the intended-vs-observed half of the mode-override block
      // (re-rendered in place by _syncScheduleDrawer). One aligned grid:
      //   (caption row)   Intended·<mode>    Observed·<mode> [badge]
      //   per register    <label>  <desired>  <observed>
      // The observed cell is coloured from its `match` flag + verify phase:
      //   match=true            → green
      //   match=false, pending  → amber (verify window still open)
      //   match=false, settled  → red   (window closed / mismatch verdict)
      //   match=null            → neutral (mode leaves this register at default)
      const st = this._hass.states[sid];
      const attrs = st?.attributes || {};
      const labels = spec.option_labels || {};
      const pretty = (v) => (v == null ? '—' : (labels[v] || v));

      const verify = attrs.verify_state || null;
      const commandedRaw = attrs.last_commanded_mode;
      const commanded = pretty(commandedRaw);

      // Prefer the dedicated observed_mode attribute (decoded from the same
      // live readbacks as register_panel); fall back to parsing the state
      // string "<mode>(reg=…)" for older sensor payloads.
      let observedRaw = attrs.observed_mode;
      if (observedRaw == null) {
        const s = st?.state ?? '';
        observedRaw = (s.match(/^([^(\s]+)/) || [null, null])[1];
      }
      let observed = pretty(observedRaw);
      // The state string lags the verify loop by up to one 5-min cycle; trust
      // the verdict — ok ⇒ observed == commanded by definition.
      if (verify === 'ok' && commandedRaw) observed = commanded;

      let badgeCls = '', badgeText = '', badgeTitle = '';
      if (verify === 'ok')            { badgeCls = 'ok';       badgeText = '✓'; badgeTitle = 'Engaged'; }
      else if (verify === 'pending')  { badgeCls = 'pending';  badgeText = '⏳'; badgeTitle = 'Verifying…'; }
      else if (verify === 'mismatch') { badgeCls = 'mismatch'; badgeText = '⚠'; badgeTitle = 'Mismatch'; }
      const badgeHtml = badgeText
        ? `<span class="verify-badge ${badgeCls}" title="${badgeTitle}">${badgeText}</span>`
        : '';

      const rows = Array.isArray(attrs.register_panel) ? attrs.register_panel : [];
      const regCells = rows.map((r) => {
        let cls = 'reg-na';
        if (r.match === true) cls = 'reg-ok';
        else if (r.match === false) cls = (verify === 'mismatch') ? 'reg-bad' : 'reg-wait';
        const label = this._REG_SHORT_LABELS[r.name] || r.label || r.name;
        return `<span class="reg-label" title="${r.label || r.name}">${label}</span>` +
          `<span class="reg-intended">${this._fmtReg(r.desired)}</span>` +
          `<span class="reg-observed ${cls}">${this._fmtReg(r.observed)}</span>`;
      }).join('');

      return `<div class="mo-table">` +
        `<span class="mo-th"></span>` +
        `<span class="mo-th"><span class="mo-cap">Intended</span>` +
          `<span class="mo-mode">${commanded}</span></span>` +
        `<span class="mo-th"><span class="mo-cap">Observed</span>` +
          `<span class="mo-mode-row"><span class="mo-mode">${observed}</span>${badgeHtml}</span></span>` +
        regCells +
      `</div>`;
    }

    _fmtReg(v) {
      // Compact render of a register value: em-dash for missing, trimmed
      // decimals for floats, as-is for ints / strings.
      if (v == null) return '—';
      if (typeof v === 'number') {
        return Number.isInteger(v) ? String(v) : v.toFixed(1);
      }
      return String(v);
    }

    _syncScheduleDrawer() {
      // In-place state refresh that preserves input focus and partial typing.
      // Falls back to a full re-render when the entity wiring changes (entity
      // appears/disappears) so the "entity not found" rows update too.
      const panel = this.shadowRoot.querySelector('#schedule-panel');
      if (!panel) return;
      const expected =
        this._SCHEDULE_SWITCHES.length +
        this._SCHEDULE_NUMBERS.length +
        this._SCHEDULE_SELECTS.length;
      const rows = panel.querySelectorAll('.sched-row');
      if (rows.length !== expected) { this._renderScheduleDrawer(); return; }

      const active = this.shadowRoot.activeElement;
      panel.querySelectorAll('input[data-kind="switch"]').forEach((el) => {
        if (el === active) return;
        const st = this._hass.states[el.dataset.eid];
        if (!st) return;
        el.checked  = st.state === 'on';
        el.disabled = st.state === 'unavailable';
      });
      panel.querySelectorAll('input[data-kind="number"]').forEach((el) => {
        if (el === active) return;
        const st = this._hass.states[el.dataset.eid];
        if (!st) return;
        const unavailable = st.state === 'unavailable' || st.state === 'unknown';
        el.disabled = unavailable;
        if (!unavailable) el.value = st.state;
      });
      // For the button-group selects, sync the `.selected` class on each
      // button against the entity state. A full re-render is unnecessary
      // because the option set doesn't change at runtime.
      // For the mode-override group only, also mirror the verify_state
      // (pending / mismatch) of the observed-mode sensor onto the selected
      // button so the operator sees the engagement state at a glance.
      const modeSpec = this._SCHEDULE_SELECTS.find(s => s.key === 'mode_override');
      const modeReadoutSid = modeSpec
        ? this._findEntityId('sensor', modeSpec.readout_sensor_match) : null;
      const modeReadoutAttrs = modeReadoutSid
        ? this._hass.states[modeReadoutSid]?.attributes : null;
      const modeVerifyState = modeReadoutAttrs?.verify_state ?? null;
      const modeLabels = modeSpec?.option_labels || {};
      panel.querySelectorAll('button[data-kind="select"]').forEach((el) => {
        const st = this._hass.states[el.dataset.eid];
        if (!st) return;
        const unavailable = st.state === 'unavailable';
        el.disabled = unavailable;
        const isSelected = !unavailable && el.dataset.option === st.state;
        el.classList.toggle('selected', isSelected);
        // Verify-state badges only meaningful for the mode-override group.
        const isModeOverride = el.dataset.eid &&
          el.dataset.eid.includes('mode_override');
        el.classList.toggle(
          'pending', isModeOverride && isSelected && modeVerifyState === 'pending'
        );
        el.classList.toggle(
          'mismatch', isModeOverride && isSelected && modeVerifyState === 'mismatch'
        );
        // Keep the sunSale sentinel's "[<mode>]" annotation in sync with the
        // mode currently being dispatched (last_commanded_mode changes between
        // schedule slots without the option set changing).
        if (isModeOverride && el.dataset.option === 'sunsale') {
          el.innerHTML = this._optionButtonLabel('sunsale', modeLabels, modeReadoutAttrs);
        }
      });
      panel.querySelectorAll('.sched-readout').forEach((el) => {
        // The mode-override readout is a structured commanded-vs-observed
        // composition (built by _renderModeReadoutInner) — re-render its
        // inner HTML in place rather than swapping a single value span.
        if (el.dataset.specKey === 'mode_override') {
          const spec = this._SCHEDULE_SELECTS.find(s => s.key === el.dataset.specKey);
          if (spec) el.innerHTML = this._renderModeReadoutInner(
            spec, el.dataset.readoutEid,
          );
          return;
        }
        const st = this._hass.states[el.dataset.readoutEid];
        const valueEl = el.querySelector('.sched-readout-value');
        if (valueEl) valueEl.textContent = st?.state ?? 'unavailable';
      });
    }

    async _onSwitchChange(el) {
      const eid = el.dataset.eid;
      const service = el.checked ? 'turn_on' : 'turn_off';
      try {
        await this._hass.callService('switch', service, { entity_id: eid });
      } catch (e) {
        console.warn('sunSale: switch service call failed', eid, e);
        // Revert visual state — HA push will re-sync on next update.
        el.checked = !el.checked;
      }
    }

    async _onNumberChange(el) {
      const eid = el.dataset.eid;
      const raw = parseFloat(el.value);
      if (!isFinite(raw)) { el.value = ''; return; }
      const min = parseFloat(el.min);
      const max = parseFloat(el.max);
      let value = raw;
      if (isFinite(min) && value < min) value = min;
      if (isFinite(max) && value > max) value = max;
      el.value = value;
      try {
        await this._hass.callService('number', 'set_value', { entity_id: eid, value });
      } catch (e) {
        console.warn('sunSale: number service call failed', eid, e);
      }
    }

    async _onSelectClick(el) {
      const eid = el.dataset.eid;
      const option = el.dataset.option;
      if (!eid || !option || el.disabled) return;
      // Optimistic UI: light up the clicked button immediately; the next HA
      // state push will reconcile if the call fails.
      const group = el.parentElement;
      if (group) {
        group.querySelectorAll('button[data-kind="select"]').forEach(b => {
          b.classList.toggle('selected', b === el);
        });
      }
      try {
        await this._hass.callService('select', 'select_option', { entity_id: eid, option });
      } catch (e) {
        console.warn('sunSale: select service call failed', eid, option, e);
      }
    }

    _setStatus(msg) {
      const el = this.shadowRoot.querySelector('#status');
      if (el) el.textContent = msg;
    }

    _clearStatus() {
      const el = this.shadowRoot.querySelector('#status');
      if (el) el.textContent = '';
    }

    // ── Data: history API — past buy/sell prices + battery SOC ────────────────
    // Folded into one request so the panel makes a single round-trip per render.
    // The observed power-flow lines (consumption · grid · losses) no longer
    // come from raw sensor history — they are read from the coordinator's
    // slotted observers via dashAttrs.observed_power_slots (see
    // _readObservedPowerSlots).

    async _fetchHistory(windowStartMs, socEntityId) {
      const start    = new Date(windowStartMs).toISOString();
      const end      = new Date().toISOString();
      const entitySet = new Set([this._buyPriceEid, this._sellPriceEid]);
      if (socEntityId) entitySet.add(socEntityId);
      const entities = Array.from(entitySet).join(',');
      const url      = `/api/history/period/${start}?end_time=${end}`
        + `&filter_entity_id=${entities}&minimal_response=true&no_attributes=true`;
      try {
        const resp = await this._hass.fetchWithAuth(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const raw = await resp.json();

        const indexed = {};
        for (const entityHistory of raw) {
          if (!entityHistory?.length) continue;
          const eid = entityHistory[0].entity_id;
          indexed[eid] = entityHistory
            .filter(s => s.state && !['unavailable', 'unknown', 'None'].includes(s.state))
            .map(s => [new Date(s.last_changed).getTime(), parseFloat(s.state)])
            .filter(([, v]) => isFinite(v));
        }
        return {
          buyPrice:   indexed[this._buyPriceEid]  || [],
          sellPrice:  indexed[this._sellPriceEid] || [],
          socHistory: socEntityId ? (indexed[socEntityId] || []) : [],
        };
      } catch (e) {
        console.warn('sunSale: history fetch failed', e);
        return { buyPrice: [], sellPrice: [], socHistory: [] };
      }
    }

    // ── Data: observed power flows (consumption · grid import/export · losses) ─
    // Reads the coordinator's slotted observers from
    // dashAttrs.observed_power_slots (see DashboardSensor._serialize_observed_power):
    // each slot's energy is already converted server-side to its average power
    // (kW) over the slot. Grid arrives pre-split into two non-negative
    // magnitudes (grid_import / grid_export); a slot may carry both, so they
    // are kept as independent series. Import is negated here so the chart draws
    // it below the zero line (power flowing in) while export stays above it
    // (power flowing out) — both directions are then visible at the same slot.
    // The series are 15-min slot averages — not the former ~10 s real-time
    // composition — so each array is sparse (≈96 points/day) and steplined.
    //
    // Returns [[epoch_ms, kw]] arrays clamped to [windowStart, nowMs] (import
    // values ≤ 0), or null when the attribute is absent (older backend / no
    // observations yet).
    _readObservedPowerSlots(dashAttrs, windowStart, nowMs) {
      const slots = dashAttrs?.observed_power_slots;
      if (!slots) return null;
      const toPoints = (arr, sign = 1) => (Array.isArray(arr) ? arr : [])
        .filter(s => s && s.t >= windowStart && s.t <= nowMs)
        .map(s => [s.t, sign * (Number(s.kw) || 0)])
        .sort((a, b) => a[0] - b[0]);
      return {
        consumption: toPoints(slots.consumption),
        gridImport:  toPoints(slots.grid_import, -1),
        gridExport:  toPoints(slots.grid_export),
        losses:      toPoints(slots.losses),
      };
    }

    // ── Data: SOC forecast from schedule plan ─────────────────────────────────
    // Returns [[timestamp_ms, soc_pct]] points: current SOC at nowTs, then
    // the expected SOC after each future plan slot.

    _buildSocForecast(dashAttrs) {
      const nowMs      = Number(dashAttrs?.now_ts) || Date.now();
      const currentSoc = dashAttrs?.battery_soc_pct;
      const plan       = Array.isArray(dashAttrs?.inverter_mode_plan)
        ? dashAttrs.inverter_mode_plan : [];
      const points = [];
      if (currentSoc != null) points.push([nowMs, currentSoc]);
      for (const slot of plan) {
        if (slot.end_t > nowMs && slot.expected_soc_after != null) {
          points.push([slot.end_t, Math.round(slot.expected_soc_after * 1000) / 10]);
        }
      }
      return points.sort((a, b) => a[0] - b[0]);
    }

    // ── Data: buy/sell prices from pricing pipeline sensor (all slots) ────────

    _readPricingSlots(beforeMs) {
      const attrs = this._hass.states[this._pricingEid]?.attributes;
      if (!Array.isArray(attrs?.slots)) return { buy: [], sell: [] };

      const buy  = [];
      const sell = [];
      for (const slot of attrs.slots) {
        const t = new Date(slot.start).getTime();
        if (!isFinite(t) || t > beforeMs) continue;
        if (typeof slot.buy_eur_kwh  === 'number') buy.push([t, slot.buy_eur_kwh]);
        if (typeof slot.sell_eur_kwh === 'number') sell.push([t, slot.sell_eur_kwh]);
      }
      return {
        buy:  buy.sort((a, b)  => a[0] - b[0]),
        sell: sell.sort((a, b) => a[0] - b[0]),
      };
    }

    // ── Data: 15-min forecast kWh slots from dashboard sensor ─────────────────

    _buildForecastSlots(dashAttrs, windowStart, windowEnd) {
      const slots = new Map();
      if (!dashAttrs) return slots;
      for (const f of (dashAttrs.forecast_slots || [])) {
        if (f.t >= windowStart && f.t <= windowEnd)
          slots.set(f.t, f.forecast_kwh ?? f.forecast_w / 1000 * 0.25);
      }
      return slots;
    }

    // ── Data: per-slot forecast accuracy from dashboard sensor ────────────────
    // Returns [{x, forecastKwh, errorKwh}] for slots inside the window with a
    // non-zero error. error_kwh = observed_kwh - forecast_kwh.

    _readForecastErrors(dashAttrs, windowStart, windowEnd) {
      if (!Array.isArray(dashAttrs?.forecast_error_slots)) return [];
      const out = [];
      for (const s of dashAttrs.forecast_error_slots) {
        if (typeof s?.t !== 'number') continue;
        if (s.t < windowStart || s.t > windowEnd) continue;
        const err = Number(s.error_kwh);
        const obs = Number(s.observed_kwh);
        // -1 sentinel from the backend = observation not yet recovered for
        // this slot. Surface as a "pending" marker; the overlay drawer
        // renders these as a thin grey strip instead of a green/red rect.
        if (err === -1 || obs === -1) {
          out.push({
            x:            s.t,
            forecastKwh:  Number(s.forecast_kwh) || 0,
            errorKwh:     0,
            pending:      true,
          });
          continue;
        }
        if (!isFinite(err) || Math.abs(err) < 1e-4) continue;
        out.push({
          x:            s.t,
          forecastKwh:  Number(s.forecast_kwh) || 0,
          errorKwh:     err,
          pending:      false,
        });
      }
      return out;
    }

    // Estimate how many rows ECharts' wrapping legend occupies at the given
    // available pixel width by greedily packing each entry's measured text
    // width. Drives the chart's dynamic top margin so the series legend never
    // spills into the plot on narrow (mobile) viewports.
    _estimateLegendRows(labels, availW) {
      if (!labels.length || !(availW > 0)) return 1;
      const ctx = (this._legendMeasureCtx ||=
        document.createElement('canvas').getContext('2d'));
      if (!ctx) return 1;
      ctx.font = '12px sans-serif';
      const ICON = 25, ICON_GAP = 5, ITEM_GAP = 12;   // ECharts legend defaults
      let rows = 1, x = 0;
      for (const label of labels) {
        const w = ICON + ICON_GAP + ctx.measureText(label).width + ITEM_GAP;
        if (x > 0 && x + w > availW) { rows++; x = w; }
        else { x += w; }
      }
      return rows;
    }

    // Compute responsive chart geometry from the live container width: the
    // grid's top margin grows with the number of wrapped legend rows (so the
    // legend never overlaps the plot) and side margins tighten on narrow
    // screens. Returns pixel values for the grid edges and total canvas height.
    _chartLayout(labels) {
      const el     = this.shadowRoot.querySelector('#chart');
      const availW = ((el && el.clientWidth) || window.innerWidth || 600) - 12;
      const narrow = availW < 560;
      // Reserve room for the top-right toolbox so the centred legend's first
      // row doesn't slide under the zoom/restore icons on narrow screens.
      const rows   = this._estimateLegendRows(labels, availW - 52);
      // 5px legend top + ~21px per legend row + 16px mode-strip lane beneath it.
      const top    = Math.max(56, 5 + rows * 21 + 16);
      const side   = narrow ? 44 : 60;
      const PLOT   = 374;   // keep the plotting band at its established height
      const bottom = 70;
      return { top, left: side, right: side, bottom, height: top + PLOT + bottom };
    }

    // Responsive geometry for a forecast-quality chart. The long descriptive
    // title is allowed to wrap (estimate its line count to reserve the top
    // margin) and the bottom-anchored 5-metric legend wraps on narrow screens
    // (reserve the bottom for it plus the rotated x-axis labels). Side margins
    // tighten on narrow viewports. Returns grid edges, title sizing, and the
    // total canvas height so the container and the option stay in sync.
    _qualityGeom(title, availW) {
      const narrow    = availW < 560;
      const side      = narrow ? 40 : 56;
      const titleFont = narrow ? 11 : 12;
      const innerW    = Math.max(80, availW - 2 * side);
      let titleLines  = 1;
      const ctx = (this._legendMeasureCtx ||=
        document.createElement('canvas').getContext('2d'));
      if (ctx) {
        ctx.font = `600 ${titleFont}px sans-serif`;
        titleLines = Math.min(3, Math.max(1,
          Math.ceil(ctx.measureText(title).width / innerW)));
      }
      const legendRows = this._estimateLegendRows(
        ['MAE (Wh)', 'RMSE (Wh)', 'Bias (Wh)', 'MAPE (%)', 'R²×100 (%)'], availW);
      const top    = 8 + titleLines * (titleFont + 4) + 8;
      const bottom = legendRows * 20 + 30;   // wrapped legend + rotated x labels
      const PLOT   = 200;                     // keep a readable plotting band
      return { narrow, side, titleFont, titleWidth: innerW, top, bottom,
               height: top + PLOT + bottom };
    }

    // Single resize path for every ECharts instance on the panel. ECharts does
    // not auto-resize, so on a container-width change (HA sidebar toggle, window
    // resize, device rotation) each chart is re-fit and its responsive geometry
    // recomputed. Skips height-only mutations (which our own height writes
    // trigger) to avoid a feedback loop, and rAF-coalesces resize bursts.
    _resizeCharts() {
      if (this._resizeRaf) return;
      this._resizeRaf = requestAnimationFrame(() => {
        this._resizeRaf = 0;
        const w = this.clientWidth;
        if (w === this._lastResizeW) return;
        this._lastResizeW = w;

        if (this._chart) {
          const el = this.shadowRoot.querySelector('#chart');
          if (el) {
            const lay = this._chartLayout(this._chartLegendLabels || []);
            el.style.height = lay.height + 'px';
            this._chart.setOption({
              grid: { left: lay.left, right: lay.right, top: lay.top, bottom: lay.bottom },
            });
            this._chart.resize();
          }
        }

        for (const qc of (this._qualityCharts || [])) {
          if (!qc.chart || !qc.el) continue;
          const aw = qc.el.clientWidth || window.innerWidth || 600;
          qc.el.style.height = this._qualityGeom(qc.title, aw).height + 'px';
          qc.chart.setOption(
            this._buildQualityChartOption(qc.title, qc.labels, qc.metrics, aw));
          qc.chart.resize();
        }
      });
    }

    // Install the shared resize observer once, on the stable host element.
    _ensureResizeObserver() {
      if (this._resizeObs) return;
      this._resizeObs = new ResizeObserver(() => this._resizeCharts());
      this._resizeObs.observe(this);
    }

    // ── Render ─────────────────────────────────────────────────────────────────

    // Guard against overlapping renders: `_renderInner` awaits a history fetch,
    // so a refresh triggered mid-render would race. Coalesce into one trailing
    // re-render instead.
    async _render() {
      if (this._rendering) { this._renderQueued = true; return; }
      this._rendering = true;
      try {
        await this._renderInner();
      } finally {
        this._rendering = false;
        if (this._renderQueued) { this._renderQueued = false; this._render(); }
      }
    }

    async _renderInner() {
      const now         = Date.now();
      const windowStart = localMidnight(-1);         // yesterday 00:00 local
      const windowEnd   = localMidnight(2) - 60_000; // tomorrow 23:59 local
      const SLOT_MS     = 15 * 60 * 1000;

      const dashAttrs     = this._hass.states[this._dashboardEid]?.attributes;
      // Mark this cycle as rendered so `_maybeRefreshCharts` won't re-trigger
      // until the coordinator pushes the next one.
      const ds = this._hass.states[this._dashboardEid];
      this._lastDashStamp = ds ? (ds.last_updated || ds.last_changed) : null;
      const socEntityId   = dashAttrs?.battery_soc_entity_id || '';
      const history       = await this._fetchHistory(windowStart, socEntityId);
      const pricingSlots  = this._readPricingSlots(windowEnd);
      const forecastSlots = this._buildForecastSlots(dashAttrs, windowStart, windowEnd);
      const errorSlots      = this._readForecastErrors(dashAttrs, windowStart, windowEnd);
      const socHistoryData  = history.socHistory.filter(([t]) => t >= windowStart && t <= now);
      const socForecastData = this._buildSocForecast(dashAttrs);
      // Observed power flows (consumption · grid import/export · losses) as
      // 15-min slot averages from the coordinator's observers, yesterday→now.
      const powerFlows = this._readObservedPowerSlots(dashAttrs, windowStart, now);
      const exportLimitKw = Number(dashAttrs?.export_limit_kw);
      // Grid import is drawn below zero, so the power band's floor drops to fit
      // the import peak (+10% headroom) when there is import this window; with
      // no import it stays at 0 so the positive flows keep the full band.
      let powerYMin = 0;
      if (powerFlows?.gridImport?.length) {
        const importPeak = Math.max(0, ...powerFlows.gridImport.map(p => -p[1]));
        if (importPeak > 0) powerYMin = -(importPeak * 1.1);
      }

      this._renderBatteryRow(dashAttrs);
      this._renderGenerationRow(dashAttrs);
      this._renderFlowDiagram(dashAttrs);

      // Merge past history + future pricing sensor into continuous price lines.
      const _merge = (histPoints, pricingPoints, windowStartMs) => {
        const histFiltered = histPoints.filter(([t]) => t >= windowStartMs);
        const histDeduped  = histFiltered.filter(([t]) =>
          !pricingPoints.some(([pt]) => Math.abs(pt - t) < 30 * 60 * 1000)
        );
        return [...histDeduped, ...pricingPoints].sort((a, b) => a[0] - b[0]);
      };

      const buyData  = _merge(history.buyPrice,  pricingSlots.buy,  windowStart);
      const sellData = _merge(history.sellPrice, pricingSlots.sell, windowStart);

      if (!buyData.length && !sellData.length) {
        this._setStatus('No price data — waiting for sunSale coordinator to run.');
        return;
      }

      const GREY_BAR = 'rgba(158,158,158,0.25)';
      const forecastBars = [];
      let t = Math.floor(windowStart / SLOT_MS) * SLOT_MS;
      while (t <= windowEnd) {
        const fKwh = forecastSlots.get(t);
        if (fKwh != null && fKwh > 0.001) {
          forecastBars.push({ x: t, y: fKwh, color: GREY_BAR });
        }
        t += SLOT_MS;
      }

      this._clearStatus();

      // ─── Inverter StorageMode bands (history yesterday→now + plan today→tomorrow).
      // History entries from inverter_mode_history are {t, mode} change events;
      // turn them into bands [event.t, next_event.t || now]. Plan slots from
      // inverter_mode_plan are {t, end_t, mode} and become bands directly.
      // StorageMode → strip color. Keys match StorageMode.value
      // (Python contract/models.py). Used by renderModeStripBlock and the
      // tooltip dot, and as the recognised-mode filter when stitching bands.
      const STORAGE_MODE_STRIP = {
        feed_in:     '#ff9800',
        self_use:    '#2196f3',
        no_export:   '#03a9f4',
        discharge:   '#f44336',
        grid_charge: '#4caf50',
        stand_by:    '#7890a0',
        auto:        '#bdbdbd',
        track:       '#9c27b0',
        unknown:     '#606060',
      };
      const modeBands = [];
      {
        const nowMs = Number(dashAttrs?.now_ts) || Date.now();
        const histRaw = Array.isArray(dashAttrs?.inverter_mode_history)
          ? [...dashAttrs.inverter_mode_history].sort((a, b) => a.t - b.t)
          : [];
        for (let i = 0; i < histRaw.length; i++) {
          const ev = histRaw[i];
          if (!STORAGE_MODE_STRIP[ev.mode]) continue;
          const nextStart = i + 1 < histRaw.length ? histRaw[i + 1].t : nowMs;
          if (nextStart <= ev.t) continue;
          modeBands.push({ mode: ev.mode, x: ev.t, x2: nextStart });
        }
        const planRaw = Array.isArray(dashAttrs?.inverter_mode_plan)
          ? dashAttrs.inverter_mode_plan
          : [];
        for (const s of planRaw) {
          if (!STORAGE_MODE_STRIP[s.mode]) continue;
          if (!(s.end_t > s.t)) continue;
          modeBands.push({ mode: s.mode, x: s.t, x2: s.end_t });
        }
      }

      const STORAGE_MODE_LABEL = {
        feed_in: 'Feed-in', self_use: 'Self-use', no_export: 'No export',
        discharge: 'Discharge', grid_charge: 'Grid charge', stand_by: 'Standby',
        auto: 'Auto', track: 'Track', unknown: 'Unknown',
      };
      const STORAGE_MODE_DOT = STORAGE_MODE_STRIP;

      const billingData = this._buildBillingSeries(windowStart, now);
      const { imported: importData, exported: exportData } =
        this._buildImportExportSeries(windowStart, now);

      // ── Vertical band layout ──────────────────────────────────────────────
      // The three primary groups are stacked into non-overlapping fraction
      // bands of the grid (0 = bottom, 1 = top): power flows occupy the lower
      // 30%, the generation/estimation bars the middle 40%, and prices the
      // upper 30%. SOC, net billing and the cumulative import/export energy
      // lines stay full-height overlays (their hidden axes already auto-fit /
      // span 0–100). `bandAxis` maps a data range [lo, hi] onto a band
      // [fLo, fHi] by returning the {min, max} whose linear scale lands the
      // data there — stacking the groups like sub-charts while keeping one
      // grid, tooltip and dataZoom. (A true multi-panel layout would use
      // separate ECharts grids; this approximates it without that machinery.)
      const bandAxis = (lo, hi, fLo, fHi) => {
        if (!(hi > lo)) hi = lo + 1;             // guard a zero / inverted range
        const span = (hi - lo) / (fHi - fLo);
        const min = lo - fLo * span;
        return { min, max: min + span };
      };

      // Pricing → upper 30%.
      const allPriceVals = [...buyData, ...sellData].map(([, v]) => v).filter(isFinite);
      const pMin = allPriceVals.length ? Math.min(...allPriceVals) : 0;
      const pMax = allPriceVals.length ? Math.max(...allPriceVals) : 0.2;
      const pPad = Math.max((pMax - pMin) * 0.05, 0.005);
      const priceLo = pMin - pPad;
      const priceHi = pMax + pPad;
      const priceBand = bandAxis(priceLo, priceHi, 0.70, 1.0);

      // Estimation (generation forecast bars + error) → middle 40%, anchored at
      // value 0 (the 30% line, i.e. the top of the power band).
      const maxForecastKwh = forecastBars.length
        ? Math.max(...forecastBars.map(b => b.y))
        : 1.0;
      const genHi = Math.ceil(maxForecastKwh * 1000 * 1.1 / 100) * 100 / 1000;
      const genBand = bandAxis(0, genHi, 0.30, 0.70);

      // Power flows → lower 30%, keeping the import-negative / export-positive
      // split. Top = max(export cap, observed positive peak) + 10% headroom.
      const powerPos = powerFlows
        ? [...powerFlows.consumption, ...powerFlows.gridExport, ...powerFlows.losses]
            .map(p => p[1]).filter(isFinite)
        : [];
      const powerHi = Math.max(
        (isFinite(exportLimitKw) && exportLimitKw > 0) ? exportLimitKw * 1.1 : 0,
        powerPos.length ? Math.max(...powerPos) * 1.1 : 0,
        0.5,
      );
      const powerBand = bandAxis(powerYMin, powerHi, 0.0, 0.30);

      // O(1) slot lookups for the tooltip formatter (avoids per-mousemove scans).
      const forecastBarBySlot = new Map(forecastBars.map(b => [b.x, b]));
      const errorBySlot       = new Map(errorSlots.map(e => [e.x, e]));
      const billingBySlot     = new Map(billingData.map(p => [p.x, p]));
      const importBySlot      = new Map(importData.map(p => [p.x, p]));
      const exportBySlot      = new Map(exportData.map(p => [p.x, p]));
      // SOC history is state-change events; use stepline lookup (same as prices).
      const findSocAt = (xMs) => {
        const d = socHistoryData;
        if (!d.length || xMs < d[0][0]) return null;
        let lo = 0, hi = d.length - 1;
        while (lo < hi) {
          const mid = (lo + hi + 1) >>> 1;
          if (d[mid][0] <= xMs) lo = mid;
          else hi = mid - 1;
        }
        return d[lo];
      };
      // SOC forecast: sorted points — find the closest future point for a given x.
      const findSocForecastAt = (xMs) => {
        for (const pt of socForecastData) {
          if (pt[0] >= xMs) return pt;
        }
        return null;
      };

      // Stepline binary search: prices are sparse, so we interpolate by holding
      // the last value whose timestamp ≤ x (stepline 'end' semantics).
      const findSteplineAt = (data, x) => {
        if (!data.length || x < data[0][0]) return null;
        let lo = 0, hi = data.length - 1;
        while (lo < hi) {
          const mid = (lo + hi + 1) >>> 1;
          if (data[mid][0] <= x) lo = mid;
          else hi = mid - 1;
        }
        return data[lo];
      };

      // Forecast bars rendered as a custom series. Native `type: 'bar'` on a
      // time axis auto-sizes bar width from data density, leaving gaps when
      // the visible range zooms in; rendering rects directly via api.coord()
      // guarantees each rect spans exactly its 15-min slot regardless of zoom.
      const renderForecastBar = (params, api) => {
        const bar = forecastBars[params.dataIndex];
        if (!bar) return;
        const xLeft  = api.coord([bar.x,           0])[0];
        const xRight = api.coord([bar.x + SLOT_MS, 0])[0];
        const yTop   = api.coord([bar.x, bar.y])[1];
        const yBot   = api.coord([bar.x, 0])[1];
        return {
          type: 'rect',
          shape: {
            x:      xLeft,
            y:      yTop,
            width:  Math.max(1, xRight - xLeft),
            height: Math.max(1, yBot - yTop),
          },
          style: { fill: bar.color },
        };
      };

      // Forecast-error overlay as a second custom series. Each rect is anchored
      // at the top of its forecast bar (y = forecast_kwh) and extends UP by
      // +error (green: under-forecast) or DOWN by |error| (red: over-forecast).
      // Pending observations (-1 sentinel) render as a thin grey strip above
      // the bar top. Width matches the forecast bar exactly (same xLeft/xRight
      // derivation), so the two custom series stay aligned at every zoom level.
      const PEND_PX = 4;
      const renderErrorRect = (params, api) => {
        const slot = errorSlots[params.dataIndex];
        if (!slot) return;
        const xLeft  = api.coord([slot.x,           0])[0];
        const xRight = api.coord([slot.x + SLOT_MS, 0])[0];
        const width  = Math.max(1, xRight - xLeft);
        if (slot.pending) {
          const yTop = api.coord([slot.x, slot.forecastKwh])[1];
          return {
            type: 'rect',
            shape: { x: xLeft, y: yTop - PEND_PX, width, height: PEND_PX },
            style: { fill: '#9e9e9e', opacity: 0.7 },
          };
        }
        const yUpper = api.coord([slot.x, Math.max(slot.forecastKwh, slot.forecastKwh + slot.errorKwh)])[1];
        const yLower = api.coord([slot.x, Math.min(slot.forecastKwh, slot.forecastKwh + slot.errorKwh)])[1];
        return {
          type: 'rect',
          shape: { x: xLeft, y: yUpper, width, height: Math.max(1, yLower - yUpper) },
          style: { fill: slot.errorKwh > 0 ? '#66bb6a' : '#ef5350', opacity: 0.85 },
        };
      };

      // Inverter-mode strip: a thin lane above the plot area that paints each
      // (history + plan) band as a solid block, so mode is always visible even
      // for slots whose forecast is zero (e.g. night, no expected generation).
      // y/height are pixel-absolute against the grid origin so the strip stays
      // fixed at the top regardless of y-axis zoom.
      const STRIP_HEIGHT_PX = 10;
      const STRIP_GAP_PX    = 6;
      const renderModeStripBlock = (params, api) => {
        const band = modeBands[params.dataIndex];
        if (!band) return;
        const xLeft  = api.coord([band.x,  0])[0];
        const xRight = api.coord([band.x2, 0])[0];
        const gridTop = params.coordSys.y;
        return {
          type: 'rect',
          shape: {
            x:      xLeft,
            y:      gridTop - STRIP_HEIGHT_PX - STRIP_GAP_PX,
            width:  Math.max(1, xRight - xLeft),
            height: STRIP_HEIGHT_PX,
          },
          style: {
            fill:   STORAGE_MODE_STRIP[band.mode] || '#606060',
            stroke: 'rgba(0,0,0,0.35)',
            lineWidth: 0.5,
          },
        };
      };

      const dot = c => `<span style="display:inline-block;width:9px;color:${c}">●</span>`;
      const sq  = c => `<span style="display:inline-block;width:9px;color:${c}">▮</span>`;

      // Power-flow palette (matches the former standalone power chart).
      const POWER_CONS = '#26c6da';   // consumption (household load)
      const POWER_GIMP = '#ffa726';   // grid import power (drawn from grid)
      const POWER_GEXP = '#5c6bc0';   // grid export power (sold to grid)
      const POWER_LOSS = '#ab47bc';   // inverter losses

      const series = [
        {
          name:       'Solar forecast',
          type:       'custom',
          yAxisIndex: 1,
          renderItem: renderForecastBar,
          data:       forecastBars.map(b => [b.x + SLOT_MS / 2, b.y]),
          itemStyle:  { color: GREY_BAR },   // legend swatch fallback
          z:          2,
          markLine:   {
            symbol: 'none',
            silent: true,
            data: [{
              xAxis: now,
              lineStyle: { color: 'rgba(255,255,255,0.55)', type: 'dashed', width: 1 },
              label: {
                show: true,
                position: 'insideEndTop',
                formatter: 'Now',
                color: '#fff',
                backgroundColor: '#333',
                padding: [3, 6, 3, 6],
                fontSize: 11,
              },
            }],
          },
        },
        {
          name:       'Buy price',
          type:       'line',
          yAxisIndex: 0,
          step:       'end',
          showSymbol: false,
          lineStyle:  { color: '#ffb300', width: 2 },
          itemStyle:  { color: '#ffb300' },
          data:       buyData,
          z:          4,
        },
        {
          name:       'Sell price',
          type:       'line',
          yAxisIndex: 0,
          step:       'end',
          showSymbol: false,
          lineStyle:  { color: '#ff7043', width: 2 },
          itemStyle:  { color: '#ff7043' },
          data:       sellData,
          z:          4,
        },
        {
          name:       'Net billing',
          type:       'line',
          yAxisIndex: 2,
          smooth:     true,
          showSymbol: false,
          lineStyle:  { color: '#66bb6a', width: 2, type: 'dashed' },
          itemStyle:  { color: '#66bb6a' },
          data:       billingData.map(p => [p.x, p.y]),
          z:          3,
        },
        {
          name:       'Grid import',
          type:       'line',
          yAxisIndex: 3,
          smooth:     true,
          showSymbol: false,
          lineStyle:  { color: '#ef5350', width: 2, type: 'dashed' },
          itemStyle:  { color: '#ef5350' },
          data:       importData.map(p => [p.x, p.y]),
          z:          3,
        },
        {
          name:       'Grid export',
          type:       'line',
          yAxisIndex: 3,
          smooth:     true,
          showSymbol: false,
          lineStyle:  { color: '#42a5f5', width: 2, type: 'dashed' },
          itemStyle:  { color: '#42a5f5' },
          data:       exportData.map(p => [p.x, p.y]),
          z:          3,
        },
        {
          name:       'Forecast error',
          type:       'custom',
          yAxisIndex: 1,
          renderItem: renderErrorRect,
          data:       errorSlots.map(e => [e.x, e.forecastKwh]),
          itemStyle:  { color: '#66bb6a' },
          tooltip:    { show: false },
          silent:     true,
          z:          5,
        },
        {
          name:       'Inverter mode',
          type:       'custom',
          yAxisIndex: 1,
          renderItem: renderModeStripBlock,
          // One datapoint per band; the x value picks the slot for ECharts'
          // tooltip slot-bucketing, while the renderItem reads the band from
          // the modeBands closure for the full (x, x2, mode) tuple.
          data:       modeBands.map(b => [b.x, 0]),
          tooltip:    { show: false },
          silent:     true,
          z:          6,
        },
        {
          name:       'Battery SOC',
          type:       'line',
          yAxisIndex: 4,
          showSymbol: false,
          lineStyle:  { color: '#4db6ac', width: 2 },
          itemStyle:  { color: '#4db6ac' },
          data:       socHistoryData,
          z:          3,
        },
        {
          name:       'SOC forecast',
          type:       'line',
          yAxisIndex: 4,
          showSymbol: false,
          lineStyle:  { color: '#4db6ac', width: 2, type: 'dashed' },
          itemStyle:  { color: '#4db6ac' },
          data:       socForecastData,
          z:          3,
        },
        // Observed power flows on a shared hidden kW axis (index 5). These are
        // 15-min slot averages (≈96 points/day) drawn as smooth lines so the
        // load curves flow rather than reading as a blocky staircase — no lttb
        // sampling needed. Grid is split into two independent lines: import
        // (negated, drops below the zero baseline) and export (rises above it),
        // so a slot that both imported and exported shows both at once.
        {
          name:       'Consumption',
          type:       'line',
          yAxisIndex: 5,
          showSymbol: false,
          smooth:     true,
          lineStyle:  { color: POWER_CONS, width: 1.5 },
          itemStyle:  { color: POWER_CONS },
          data:       powerFlows?.consumption || [],
          z:          2,
        },
        {
          name:       'Grid import (kW)',
          type:       'line',
          yAxisIndex: 5,
          showSymbol: false,
          smooth:     true,
          lineStyle:  { color: POWER_GIMP, width: 1.5 },
          itemStyle:  { color: POWER_GIMP },
          data:       powerFlows?.gridImport || [],
          z:          2,
        },
        {
          name:       'Grid export (kW)',
          type:       'line',
          yAxisIndex: 5,
          showSymbol: false,
          smooth:     true,
          lineStyle:  { color: POWER_GEXP, width: 1.5 },
          itemStyle:  { color: POWER_GEXP },
          data:       powerFlows?.gridExport || [],
          z:          2,
        },
        {
          name:       'Inverter losses',
          type:       'line',
          yAxisIndex: 5,
          showSymbol: false,
          smooth:     true,
          lineStyle:  { color: POWER_LOSS, width: 1.5 },
          itemStyle:  { color: POWER_LOSS },
          data:       powerFlows?.losses || [],
          z:          2,
        },
      ];

      // Responsive geometry: the legend wraps to more rows on narrow screens,
      // so reserve the top margin dynamically (top already includes the ~16 px
      // inverter-mode strip lane — see renderModeStripBlock) and grow the
      // canvas height to match, keeping the legend out of the plot on mobile.
      const legendLabels = ['Solar forecast', 'Buy price', 'Sell price', 'Net billing', 'Grid import', 'Grid export', 'Battery SOC', 'SOC forecast', 'Consumption', 'Grid import (kW)', 'Grid export (kW)', 'Inverter losses'];
      this._chartLegendLabels = legendLabels;
      const layout = this._chartLayout(legendLabels);

      const option = {
        backgroundColor: 'transparent',
        animation:       false,
        textStyle:       { fontFamily: 'inherit', color: '#aaa' },
        grid: {
          left:   layout.left,
          right:  layout.right,
          top:    layout.top,
          bottom: layout.bottom,
        },
        legend: {
          show:        true,
          textStyle:   { color: '#aaa' },
          data:        legendLabels,
          top:         5,
        },
        xAxis: {
          type: 'time',
          min:  windowStart,
          max:  windowEnd,
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { show: true, lineStyle: { color: 'rgba(255,255,255,0.07)' } },
          axisLabel: {
            color:    '#aaa',
            fontSize: 10,
            rotate:   -30,
            formatter: (val) => {
              const d = new Date(val);
              const dd  = String(d.getDate()).padStart(2, '0');
              const mon = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getMonth()];
              const hh  = String(d.getHours()).padStart(2, '0');
              const mm  = String(d.getMinutes()).padStart(2, '0');
              return `${dd} ${mon} ${hh}:${mm}`;
            },
          },
        },
        // Six y-axes, banded into vertical thirds (see bandAxis above):
        // 0=price (left, upper 30%), 1=forecast kWh (right, middle 40%),
        // 2=billing currency (hidden, full height), 3=import/export kWh (hidden,
        // full height), 4=SOC % (hidden, full height), 5=power kW (hidden,
        // lower 30%). The price/gen axis labels are clamped to their own band
        // so the rest of the left/right edge isn't littered with extrapolated
        // ticks; the hidden full-height overlays (billing/energy/SOC) cross all
        // bands as reference lines.
        yAxis: [
          {
            type:     'value',
            name:     `${this._currency} / kWh`,
            nameTextStyle: { color: '#ffb300', fontSize: 11 },
            position: 'left',
            min:      priceBand.min,
            max:      priceBand.max,
            axisLine: { show: false },
            axisTick: { show: false },
            splitLine: { lineStyle: { color: 'rgba(255,255,255,0.07)' } },
            axisLabel: {
              color:    '#ffb300',
              fontSize: 10,
              // Only label ticks inside the actual price band (upper 30%).
              formatter: v => (v != null && v >= priceLo - 1e-9 && v <= priceHi + 1e-9)
                ? v.toFixed(3) : '',
            },
          },
          {
            type:     'value',
            name:     'kWh / slot',
            nameTextStyle: { color: '#cfcfcf', fontSize: 11 },
            position: 'right',
            min:      genBand.min,
            max:      genBand.max,
            axisLine: { show: false },
            axisTick: { show: false },
            splitLine: { show: false },
            axisLabel: {
              color:    '#cfcfcf',
              fontSize: 10,
              // Only label ticks inside the generation band (0 → genHi).
              formatter: v => (v != null && v >= -1e-9 && v <= genHi + 1e-9)
                ? v.toFixed(3) : '',
            },
          },
          { type: 'value', position: 'right', show: false },
          { type: 'value', position: 'right', show: false },
          { type: 'value', position: 'right', show: false, min: 0, max: 100 },
          // Power kW (consumption · grid import/export · losses), confined to
          // the lower 30% band. Within it, consumption/losses/export rise above
          // the power-zero line and grid import drops below it (powerYMin =
          // −(import peak + 10%)); powerHi (export cap or observed peak + 10%)
          // pins the band ceiling. See bandAxis / powerBand above.
          { type: 'value', position: 'right', show: false, min: powerBand.min, max: powerBand.max },
        ],
        dataZoom: [
          { type: 'inside', xAxisIndex: 0, filterMode: 'none' },
          {
            type:       'slider',
            xAxisIndex: 0,
            filterMode: 'none',
            bottom:     8,
            height:     20,
            startValue: windowStart,
            endValue:   windowEnd,
            backgroundColor:      'rgba(255,255,255,0.04)',
            fillerColor:          'rgba(255,255,255,0.10)',
            borderColor:          'transparent',
            handleStyle:          { color: '#888' },
            moveHandleStyle:      { color: '#888' },
            textStyle:            { color: '#888' },
            dataBackground:       { lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 } },
            selectedDataBackground: { lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 } },
          },
        ],
        toolbox: {
          right: 20,
          top:   5,
          iconStyle: { borderColor: '#aaa' },
          feature: {
            dataZoom: { yAxisIndex: 'none' },
            restore:  {},
          },
        },
        tooltip: {
          trigger:        'axis',
          axisPointer:    { type: 'line', lineStyle: { color: 'rgba(255,255,255,0.35)' } },
          backgroundColor: '#1f1f1f',
          borderColor:    '#555',
          textStyle:      { color: '#e0e0e0', fontSize: 12 },
          padding:        [8, 12],
          extraCssText:   'min-width:240px; line-height:1.55; box-shadow:0 4px 12px rgba(0,0,0,0.4);',
          formatter: (params) => {
            const hoveredX = Array.isArray(params)
              ? (params[0]?.axisValue)
              : params.axisValue;
            if (hoveredX == null) return '';
            const xMs   = typeof hoveredX === 'number' ? hoveredX : new Date(hoveredX).getTime();
            const slotT = Math.floor(xMs / SLOT_MS) * SLOT_MS;
            const timeStr = new Date(xMs).toLocaleString([], {
              day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
              hour12: false,
            });
            const lines = [];

            const buyPt = findSteplineAt(buyData, xMs);
            if (buyPt) lines.push(`<div>${dot('#ffb300')} Buy price: <strong>${buyPt[1].toFixed(4)}</strong> ${this._currencySymbol}/kWh</div>`);

            const sellPt = findSteplineAt(sellData, xMs);
            if (sellPt) lines.push(`<div>${dot('#ff7043')} Sell price: <strong>${sellPt[1].toFixed(4)}</strong> ${this._currencySymbol}/kWh</div>`);

            const fBar = forecastBarBySlot.get(slotT);
            if (fBar) {
              const line = `<div>${dot(fBar.color)} Solar forecast: <strong>${fBar.y.toFixed(3)}</strong> kWh</div>`;
              lines.push(line);
            }

            const errSlot = errorBySlot.get(slotT);
            if (errSlot) {
              if (errSlot.pending) {
                lines.push(`<div style="padding-left:14px;font-size:11px;color:#9e9e9e">↳ Forecast error: pending observation</div>`);
              } else {
                const sign  = errSlot.errorKwh >= 0 ? '+' : '';
                const color = errSlot.errorKwh > 0 ? '#66bb6a' : '#ef5350';
                lines.push(`<div style="padding-left:14px;font-size:11px;color:${color}">↳ Forecast error: ${sign}${errSlot.errorKwh.toFixed(3)} kWh</div>`);
              }
            }

            const billPt = billingBySlot.get(slotT);
            if (billPt) {
              const sign = billPt.y >= 0 ? '+' : '';
              lines.push(`<div>${dot('#66bb6a')} Net total: <strong>${sign}${billPt.y.toFixed(2)}</strong> ${this._currencySymbol}</div>`);
            }

            const impPt = importBySlot.get(slotT);
            if (impPt) lines.push(`<div>${dot('#ef5350')} Import total: <strong>${impPt.y.toFixed(2)}</strong> kWh</div>`);

            const expPt = exportBySlot.get(slotT);
            if (expPt) lines.push(`<div>${dot('#42a5f5')} Export total: <strong>${expPt.y.toFixed(2)}</strong> kWh</div>`);

            const modeAt = modeBands.find(b => xMs >= b.x && xMs < b.x2);
            if (modeAt) {
              const dotColor = STORAGE_MODE_DOT[modeAt.mode] || '#9e9e9e';
              const label    = STORAGE_MODE_LABEL[modeAt.mode] || modeAt.mode;
              lines.push(`<div>${sq(dotColor)} Inverter mode: <strong>${label}</strong></div>`);
            }

            if (xMs <= now) {
              const socPt = findSocAt(xMs);
              if (socPt) lines.push(`<div>${dot('#4db6ac')} Battery SOC: <strong>${socPt[1].toFixed(1)}</strong>%</div>`);
            } else {
              const socFcPt = findSocForecastAt(xMs);
              if (socFcPt) lines.push(`<div>${dot('#4db6ac')} SOC forecast: <strong>${socFcPt[1].toFixed(1)}</strong>%</div>`);
            }

            if (xMs <= now && powerFlows) {
              const consPt = findSteplineAt(powerFlows.consumption, xMs);
              if (consPt) lines.push(`<div>${dot(POWER_CONS)} Consumption: <strong>${consPt[1].toFixed(2)}</strong> kW</div>`);
              const gImpPt = findSteplineAt(powerFlows.gridImport, xMs);
              if (gImpPt) lines.push(`<div>${dot(POWER_GIMP)} Grid import: <strong>${Math.abs(gImpPt[1]).toFixed(2)}</strong> kW</div>`);
              const gExpPt = findSteplineAt(powerFlows.gridExport, xMs);
              if (gExpPt) lines.push(`<div>${dot(POWER_GEXP)} Grid export: <strong>${gExpPt[1].toFixed(2)}</strong> kW</div>`);
              const lossPt = findSteplineAt(powerFlows.losses, xMs);
              if (lossPt) lines.push(`<div>${dot(POWER_LOSS)} Inverter losses: <strong>${lossPt[1].toFixed(2)}</strong> kW</div>`);
            }

            if (!lines.length) return '';
            return `<div style="font-size:11px;color:#aaa;margin-bottom:6px;padding-bottom:4px;border-bottom:1px solid #444;">${timeStr}</div>${lines.join('')}`;
          },
        },
        series,
      };

      const el = this.shadowRoot.querySelector('#chart');
      el.style.height = layout.height + 'px';
      // Reuse the instance across cycles (init once) and replace its data with
      // notMerge=true, so periodic re-renders update in place instead of
      // disposing + re-creating the canvas on every coordinator push. Capture
      // the user's current zoom/pan first and restore it afterwards, so a
      // background refresh doesn't yank them back to the full window.
      let priorZoom = null;
      if (this._chart) {
        const dz = this._chart.getOption()?.dataZoom;
        if (Array.isArray(dz) && dz.length && typeof dz[0].start === 'number') {
          priorZoom = { start: dz[0].start, end: dz[0].end };
        }
      } else {
        this._chart = window.echarts.init(el, null, { renderer: 'canvas' });
      }
      this._chart.setOption(option, true);
      if (priorZoom && !(priorZoom.start === 0 && priorZoom.end === 100)) {
        this._chart.dispatchAction({
          type: 'dataZoom', dataZoomIndex: 0,
          start: priorZoom.start, end: priorZoom.end,
        });
      }

      // Track container-width changes (HA sidebar toggle, window resize, device
      // rotation) via one shared observer on the host that re-fits and re-lays
      // out every chart. Installed once; it also self-heals a first render that
      // measured a zero-width (not-yet-laid-out) container.
      this._ensureResizeObserver();

      this._renderBillSummary();

      const dashAttrsForQuality = this._hass.states[this._dashboardEid]?.attributes;
      this._renderAccuracySection(dashAttrsForQuality?.forecast_quality ?? null);
    }

    // ── Net Billing ────────────────────────────────────────────────────────────

    // Build running-total {x, y} points for the main chart from windowStart to nowMs.
    _buildBillingSeries(windowStart, nowMs) {
      const billAttrs = this._hass.states[this._monthlyBillEid]?.attributes;
      if (!billAttrs || !Array.isArray(billAttrs.slots) || !billAttrs.slots.length) return [];

      const { carry_eur = 0, slots } = billAttrs;
      const sorted = [...slots].sort((a, b) =>
        new Date(a.start).getTime() - new Date(b.start).getTime()
      );

      let running = carry_eur;
      const data = [];
      for (const s of sorted) {
        const t = new Date(s.start).getTime();
        running += s.net_cost_eur ?? 0;
        if (t >= windowStart && t <= nowMs) {
          data.push({ x: t, y: running });
        }
      }
      return data;
    }

    // Build running-total {x, y} points for gross import + export kWh from
    // monthly_bill slots. Same shape as _buildBillingSeries — the slots run
    // from yesterday-or-month-start through now, so the cumulative line
    // resets to 0 at the start of that window each cycle.
    _buildImportExportSeries(windowStart, nowMs) {
      const billAttrs = this._hass.states[this._monthlyBillEid]?.attributes;
      if (!billAttrs || !Array.isArray(billAttrs.slots) || !billAttrs.slots.length) {
        return { imported: [], exported: [] };
      }

      const sorted = [...billAttrs.slots].sort((a, b) =>
        new Date(a.start).getTime() - new Date(b.start).getTime()
      );

      let runImp = 0, runExp = 0;
      const imported = [], exported = [];
      for (const s of sorted) {
        const t = new Date(s.start).getTime();
        runImp += s.imported_kwh ?? 0;
        runExp += s.exported_kwh ?? 0;
        if (t >= windowStart && t <= nowMs) {
          imported.push({ x: t, y: runImp });
          exported.push({ x: t, y: runExp });
        }
      }
      return { imported, exported };
    }

    _renderBillSummary() {
      const container = this.shadowRoot.querySelector('#bill');
      if (!container) return;

      const billAttrs = this._hass.states[this._monthlyBillEid]?.attributes;
      if (!billAttrs || !Array.isArray(billAttrs.slots) || !billAttrs.slots.length) {
        container.innerHTML = '<div class="bill-title">Net Billing</div>'
          + '<div class="bill-summary" style="color:#666">No billing data yet.</div>';
        return;
      }

      const {
        carry_eur, yday_to_now_eur, total_month_eur, month_str,
        previous_month_str, previous_month_eur,
      } = billAttrs;

      const fmt2 = v => (v >= 0 ? '+' : '') + v.toFixed(2) + ' ' + this._currencySymbol;
      const prevHtml = (previous_month_str)
        ? `<span class="bill-sep">·</span>`
          + `<span class="bill-label">Prev ${previous_month_str}:</span>`
          + `<span class="bill-value">${fmt2(previous_month_eur)}</span>`
        : '';

      container.innerHTML = `
        <div class="bill-title">Net Billing — ${month_str}</div>
        <div class="bill-summary">
          <span class="bill-label">Carry:</span>
          <span class="bill-value">${fmt2(carry_eur)}</span>
          <span class="bill-sep">·</span>
          <span class="bill-label">Live:</span>
          <span class="bill-value">${fmt2(yday_to_now_eur)}</span>
          <span class="bill-sep">·</span>
          <span class="bill-label">Total:</span>
          <span class="bill-value ${total_month_eur < 0 ? 'revenue' : ''}">${fmt2(total_month_eur)}</span>
          ${prevHtml}
        </div>
      `;
    }

    // ── Forecast Quality Charts ────────────────────────────────────────────────

    _buildQualityChartOption(title, xLabels, metricsArr, availW) {
      // metricsArr: [{n, bias_wh, mae_wh, rmse_wh, mape_pct, r2}, ...] aligned with xLabels.
      const get = (key) => metricsArr.map(m => (m && m[key] != null) ? m[key] : null);

      const maeSeries  = get('mae_wh');
      const rmseSeries = get('rmse_wh');
      const biasSeries = get('bias_wh');
      const mapeSeries = get('mape_pct');
      const r2Series   = get('r2').map(v => v != null ? +(v * 100).toFixed(2) : null);

      const UNIT = ['Wh', 'Wh', 'Wh', '%', '%'];
      const g    = this._qualityGeom(title, availW || 600);

      return {
        backgroundColor: 'transparent',
        animation:       false,
        textStyle:       { fontFamily: 'inherit', color: '#aaa' },
        title: {
          text:      title,
          // Wrap the long descriptive title within the plot width instead of
          // letting it clip off the canvas edges on narrow screens.
          textStyle: { color: '#aaa', fontSize: g.titleFont, fontWeight: 600,
                       width: g.titleWidth, overflow: 'break',
                       lineHeight: g.titleFont + 4 },
          top:       0,
          left:      'center',
        },
        grid: { left: g.side, right: g.side, top: g.top, bottom: g.bottom },
        legend: {
          show:      true,
          textStyle: { color: '#aaa' },
          bottom:    0,
        },
        tooltip: {
          trigger:         'axis',
          axisPointer:     { type: 'shadow' },
          backgroundColor: '#1f1f1f',
          borderColor:     '#555',
          textStyle:       { color: '#e0e0e0', fontSize: 12 },
          padding:         [8, 12],
          formatter: (params) => {
            const head = `<div style="font-size:11px;color:#aaa;margin-bottom:4px;padding-bottom:4px;border-bottom:1px solid #444;">${params[0]?.axisValueLabel ?? ''}</div>`;
            const rows = params.map(p => {
              if (p.value == null) return '';
              const swatch = `<span style="display:inline-block;width:9px;color:${p.color}">●</span>`;
              return `<div>${swatch} ${p.seriesName}: <strong>${(+p.value).toFixed(1)}</strong> ${UNIT[p.seriesIndex] || ''}</div>`;
            }).filter(Boolean).join('');
            return head + rows;
          },
        },
        xAxis: {
          type:      'category',
          data:      xLabels,
          axisLine:  { show: false },
          axisTick:  { show: false },
          axisLabel: { color: '#aaa', fontSize: g.narrow ? 9 : 10, rotate: -30 },
        },
        yAxis: [
          {
            type:          'value',
            name:          'Wh',
            nameTextStyle: { color: '#ffb300', fontSize: 10 },
            position:      'left',
            axisLine:      { show: false },
            axisTick:      { show: false },
            splitLine:     { lineStyle: { color: 'rgba(255,255,255,0.07)' } },
            axisLabel: {
              color:    '#ffb300',
              fontSize: 10,
              formatter: v => (v != null ? v.toFixed(1) : ''),
            },
          },
          {
            type:          'value',
            name:          '%',
            nameTextStyle: { color: '#66bb6a', fontSize: 10 },
            position:      'right',
            axisLine:      { show: false },
            axisTick:      { show: false },
            splitLine:     { show: false },
            axisLabel: {
              color:    '#66bb6a',
              fontSize: 10,
              formatter: v => (v != null ? v.toFixed(1) : ''),
            },
          },
        ],
        series: [
          {
            name:       'MAE (Wh)',
            type:       'bar',
            yAxisIndex: 0,
            data:       maeSeries,
            itemStyle:  { color: '#ffb300', opacity: 0.8 },
            barWidth:   '60%',
          },
          {
            name:       'RMSE (Wh)',
            type:       'line',
            yAxisIndex: 0,
            smooth:     true,
            showSymbol: false,
            data:       rmseSeries,
            lineStyle:  { color: '#ff7043', width: 2, type: 'dashed' },
            itemStyle:  { color: '#ff7043' },
          },
          {
            name:       'Bias (Wh)',
            type:       'line',
            yAxisIndex: 0,
            smooth:     true,
            showSymbol: false,
            data:       biasSeries,
            lineStyle:  { color: '#42a5f5', width: 2, type: 'dotted' },
            itemStyle:  { color: '#42a5f5' },
          },
          {
            name:       'MAPE (%)',
            type:       'line',
            yAxisIndex: 1,
            smooth:     true,
            showSymbol: false,
            data:       mapeSeries,
            lineStyle:  { color: '#66bb6a', width: 2 },
            itemStyle:  { color: '#66bb6a' },
          },
          {
            name:       'R²×100 (%)',
            type:       'line',
            yAxisIndex: 1,
            smooth:     true,
            showSymbol: false,
            data:       r2Series,
            lineStyle:  { color: '#ab47bc', width: 2, type: 'dashed' },
            itemStyle:  { color: '#ab47bc' },
          },
        ],
      };
    }

    _renderAccuracySection(quality) {
      const container = this.shadowRoot.querySelector('#accuracy');
      if (!container) return;

      // Dispose previous charts up front so both the no-data and the rebuild
      // paths drop stale instances (and clear the resize registry).
      if (this._g1Chart) { this._g1Chart.dispose(); this._g1Chart = null; }
      if (this._g2Chart) { this._g2Chart.dispose(); this._g2Chart = null; }
      if (this._g3Chart) { this._g3Chart.dispose(); this._g3Chart = null; }
      this._qualityCharts = [];

      if (!quality || (!Object.keys(quality.group1 || {}).length &&
                       !Object.keys(quality.group2 || {}).length &&
                       !Object.keys(quality.group3 || {}).length)) {
        container.innerHTML = '<div class="accuracy-title">Forecast Quality</div>'
          + '<div class="accuracy-subtitle" style="color:#666">No quality data yet — accumulates over time.</div>';
        return;
      }

      const sunriseStr = quality.sunrise_utc
        ? new Date(quality.sunrise_utc).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', hour12: false})
        : '—';
      const sunsetStr = quality.sunset_utc
        ? new Date(quality.sunset_utc).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', hour12: false})
        : '—';

      container.innerHTML = `
        <div class="accuracy-title">Forecast Quality</div>
        <div class="accuracy-subtitle">Sunrise ${sunriseStr} · Sunset ${sunsetStr} (local)</div>
        <div class="accuracy-subtitle">EMA α=0.1 running accuracy per bucket</div>
        <div id="g1-chart" class="accuracy-chart" style="height:300px"></div>
        <div id="g2-chart" class="accuracy-chart" style="height:300px"></div>
        <div id="g3-chart" class="accuracy-chart" style="height:300px"></div>
      `;

      // Initialise one quality chart: size its container to the responsive
      // height, build the width-aware option, and register it for resize.
      const mount = (selector, title, labels, metrics) => {
        const el = container.querySelector(selector);
        if (!el) return null;
        const availW = el.clientWidth || window.innerWidth || 600;
        el.style.height = this._qualityGeom(title, availW).height + 'px';
        const chart = window.echarts.init(el, null, { renderer: 'canvas' });
        chart.setOption(this._buildQualityChartOption(title, labels, metrics, availW));
        this._qualityCharts.push({ chart, el, title, labels, metrics });
        return chart;
      };

      // Group 1: intensity bins — sorted numerically by Wh.
      {
        const g1 = quality.group1 || {};
        const keys = Object.keys(g1).map(Number).sort((a, b) => a - b);
        const labels = keys.map(k => k + ' Wh');
        const metrics = keys.map(k => g1[String(k)]);
        this._g1Chart = mount('#g1-chart',
          'Group 1 — Accuracy by Predicted Intensity (per forecast-kWh bin)',
          labels, metrics);
      }

      // Group 2: solar-day positional buckets — sorted 1..N.
      {
        const g2 = quality.group2 || {};
        const keys = Object.keys(g2).map(Number).sort((a, b) => a - b);
        const n = keys.length;
        const half = Math.ceil(n / 2);
        const labels = keys.map(k => {
          if (k <= half) return `Dawn +${k - 1}`;
          return `Dusk -${n - k}`;
        });
        const metrics = keys.map(k => g2[String(k)]);
        this._g2Chart = mount('#g2-chart',
          'Group 2 — Accuracy by Solar-Day Position (Dawn → Dusk)',
          labels, metrics);
      }

      // Group 3: horizon buckets d0–d6.
      {
        const g3 = quality.group3 || {};
        const keys = Object.keys(g3).map(Number).sort((a, b) => a - b);
        const labels = keys.map(k => `d${k}`);
        const metrics = keys.map(k => g3[String(k)]);
        this._g3Chart = mount('#g3-chart',
          'Group 3 — Accuracy by Forecast Horizon (d0 = same day, d6 = 6 days ahead)',
          labels, metrics);
      }
    }
  }

  customElements.define('sun-sale-panel', SunSalePanel);
})();
