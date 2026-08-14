"""Solis Remote Dispatch driver — forced modes via the 44100 block.

Solis inverters that advertise Remote Dispatch (input register 34502 reads
``0xAA55``) accept a goal-seeking control block at holding registers
44100–44112, with an **inverter-side failsafe**: the inverter reverts on its own
if the controller stops refreshing. That is a strictly better home for sunSale's
two forced modes than the Remote-Control (RC) path they used before, for three
reasons the RC path could not fix:

  * **The deadman is ours to size.** RC's timeout register (43282) only latches
    when the force-charge/discharge enable (43135) is written first
    (Pho3niX90/solis_modbus#352); without it the firmware falls back to ~5 min
    regardless of what was asked, so a held Discharge died minutes in while
    every register still read "engaged". Dispatch's failsafe (44101) is written
    as part of the same block and holds for as long as it says.
  * **The writes are raw.** ``solis_modbus`` drives the block with
    ``async_write_holding_register(s)`` from its own service, so neither the
    ``number`` entity's same-value short-circuit nor its advertised min/max
    applies. The RC setpoint's declared ±10 kW ceiling — an entity bound, not a
    hardware one — stops constraining the commanded magnitude.
  * **It is one atomic intent.** Mode, power, SOC window and failsafe land as
    two contiguous block writes in the order the firmware requires, rather than
    a sequence of independent register writes any one of which can be dropped.

Scope is deliberately narrow. This driver **wraps** :class:`..outbound.solis_driver.SolisDriver`
and overrides only the two RC-backed modes (Discharge, GridCharge); every
passive mode keeps the register path that already works, and every read —
spec composition, observed-mode decoding, capability projection — delegates
unchanged. Selection is gated in :func:`..outbound.driver_factory.make_inverter_driver`
on both the service being installed and the inverter advertising the capability,
so an older ``solis_modbus`` or a non-dispatch inverter silently keeps the RC
path.

**Shared ownership warning.** The 44100 block is a single-writer resource:
SolisCloud's EMS drives the same registers. sunSale releases dispatch whenever a
passive mode is commanded, so the cloud can hold the fallback while sunSale is
idle — but while sunSale holds a forced mode, a foreign write shows up as
register drift and is re-commanded by the control module's reconcile path.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later

from ..contract.models import InverterCapability, InverterModeReading, StorageMode
from .driver import ControlRow, ControlStatus
from .solis_driver import SolisDriver
from .storage_mode_specs import StorageModeSpec

_LOGGER = logging.getLogger(__name__)

_DOMAIN = "solis_modbus"
SERVICE_DISPATCH = "solis_dispatch"
SERVICE_DISPATCH_STOP = "solis_dispatch_stop"

# Input register 34502 reads this when the inverter supports Remote Dispatch.
DISPATCH_CAPABLE_MAGIC = 0xAA55

# Failsafe window written to 44101 (minutes). The inverter reverts to its own
# control if sunSale stops refreshing for this long, so it is the blast radius
# of sunSale dying mid-slot: one decision window plus margin. Every holding tick
# (~5 min) re-pushes the block, giving 4× headroom — no driver-side heartbeat is
# needed, which is the whole point of moving off RC's ~5-min expiry.
_FAILSAFE_MINUTES = 20

# Delay before a freshly-commanded dispatch is written a second time. The
# inverter initialises the block's control mode / power target to its defaults
# (1 / 0) as it *enters* the dispatch state, which the enable write itself
# triggers: a mode+target write that arrives before the firmware has finished
# entering is silently discarded, leaving dispatch "running" with a zero
# setpoint. Long enough to be past that transition, short enough that a slot
# loses seconds rather than a whole cycle. See :meth:`SolisDispatchDriver._schedule_reconfirm`.
_RECONFIRM_DELAY_S = 10

# Control-mode codes read back from 44105. 3 = PCC (meter) power target, the
# grid-side goal both forced modes use: the planner prices energy at the meter,
# so holding a meter-side target is what its arithmetic assumes. Battery-side
# targets (code 2) are deliberately unused — no sunSale mode wants to fix
# battery power while letting grid flow float.
_MODE_PCC_TARGET = 3

# StorageMode → the ``solis_dispatch`` mode name driving it. Only the two modes
# that were RC-backed appear; everything else stays on the register path.
_DISPATCH_MODES: dict[StorageMode, str] = {
    StorageMode.Discharge: "grid_export",
    StorageMode.GridCharge: "grid_import",
}


class SolisDispatchDriver:
    """A :class:`..outbound.solis_driver.SolisDriver` whose forced modes use Remote Dispatch.

    Delegates everything it does not override — spec composition, decoding,
    observation, capability, grid power — to the wrapped register driver, so
    this class holds only the dispatch write path and its control-surface rows.
    """

    def __init__(
        self,
        base: SolisDriver,
        hass: HomeAssistant,
        entity_ids: dict[str, str] | None = None,
    ) -> None:
        """Wrap a register driver with the dispatch write path.

        Args:
            base: The register-level Solis driver handling passive modes and
                every read.
            hass: Home Assistant instance, for the ``solis_modbus`` service
                calls and the dispatch readback states.
            entity_ids: Role→entity-id map; the dispatch readback roles
                (``dispatch_active`` / ``dispatch_control_mode`` /
                ``dispatch_power_target`` / ``dispatch_failsafe_interval``) are
                read from it for the control surface. Missing roles degrade to
                ``unknown`` rows, exactly like any other unreadable readback.
        """
        self._base = base
        self._hass = hass
        self._entity_ids = dict(entity_ids or {})
        # What the last dispatch commanded, so ``control_surface`` can compare
        # the readback against it. ``None`` while no forced mode is held.
        self._commanded: tuple[int, int] | None = None
        # One-shot re-write of a freshly-commanded dispatch (see
        # ``_schedule_reconfirm``): its cancel-callback and the payload to
        # re-send. At most one is ever pending.
        self._reconfirm_cancel: Callable[[], None] | None = None
        self._reconfirm_payload: dict[str, Any] | None = None
        # Resolved support, latched only once positive. ``None`` = not yet
        # determined. See :meth:`_supported`.
        self._support: bool | None = None

    @property
    def control_path(self) -> str:
        """Return which write path is driving the forced modes — a panel diagnostic.

        Reflects the *resolved* answer, so an operator debugging a discharge can
        see whether this install is actually using Remote Dispatch or has fallen
        back to the RC registers.
        """
        return "dispatch" if self._supported() else "rc"

    def _supported(self) -> bool:
        """Return whether Remote Dispatch can be used, resolving lazily.

        **Not answered at construction.** Both halves of the gate depend on
        another integration being further along than sunSale: Home Assistant
        sets custom integrations up concurrently, so ``solis_modbus`` may not
        have registered ``solis_dispatch`` yet when sunSale's config entry
        loads, and the capability sensor sits on a SLOW poll group that has not
        necessarily produced a value. Deciding once at setup therefore loses the
        race on a cold boot and pins the install to the RC path until the next
        restart — which is exactly what happened on the reference install
        (2026-08-04: register 34502 read ``0xAA55`` throughout, every dispatch
        role resolved, and ``control_path`` still came up ``rc``).

        A negative answer is therefore **never cached** — it may simply be
        "not yet" — while a positive one is latched, both to avoid re-probing
        every cycle and to keep ``control_path`` from flapping.

        Returns:
            ``True`` when the dispatch path may be used.
        """
        if self._support:
            return True
        self._support = dispatch_supported(self._hass, self._entity_ids)
        return self._support

    # --- Delegated surface ---------------------------------------------- #

    def spec_for(self, mode: StorageMode) -> StorageModeSpec | None:
        """Return the effective register spec (delegated)."""
        return self._base.spec_for(mode)

    def declared_spec(self, mode: StorageMode) -> StorageModeSpec | None:
        """Return the unreduced composed spec (delegated)."""
        return self._base.declared_spec(mode)

    def effective_spec(self, mode: StorageMode) -> StorageModeSpec | None:
        """Return the bound-reduced spec (delegated)."""
        return self._base.effective_spec(mode)

    def capability(self, now: datetime) -> InverterCapability:
        """Return the hardware envelope, with the RC leg lifted while dispatching.

        The RC active-power setpoint's ±10 kW is an *entity* bound that the
        dispatch service does not go through, so on that path it no longer
        constrains what can be commanded; the battery-current and export-cap
        legs still do. While the gate is unresolved the RC leg still applies —
        reporting an envelope the RC fallback cannot deliver would have the
        planner budget export the dispatcher then clamps.

        This is also the call that resolves the gate in practice: the
        coordinator invokes it every cycle, so support is picked up as soon as
        ``solis_modbus`` finishes starting, without waiting for a forced mode.

        Args:
            now: Cycle timestamp recorded as ``resolved_at``.

        Returns:
            The resolved :class:`InverterCapability`.
        """
        if self._supported():
            return self._base.capability_without_rc_leg(now)
        return self._base.capability(now)

    def decode_observed(self) -> StorageMode:
        """Decode the live StorageMode (delegated)."""
        return self._base.decode_observed()

    def observe(self, now: datetime) -> InverterModeReading:
        """Return this cycle's observation (delegated)."""
        return self._base.observe(now)

    def observed_raw_state(self) -> int | None:
        """Return the raw 43110 readback (delegated)."""
        return self._base.observed_raw_state()

    def get_grid_power(self) -> float:
        """Return grid power in kW, positive = importing (delegated)."""
        return self._base.get_grid_power()

    # --- Write side ------------------------------------------------------ #

    async def apply_mode(
        self, mode: StorageMode, spec: StorageModeSpec, force: bool = False
    ) -> None:
        """Drive ``mode``, using Remote Dispatch for the forced modes.

        A forced mode is one atomic dispatch call. Every other mode releases
        dispatch **first** — leaving it engaged would have the inverter chase a
        stale power target underneath the register-level mode — and then applies
        through the register path unchanged.

        Args:
            mode: Target storage mode.
            spec: Effective register spec (used by the delegated path; the
                dispatch magnitude comes from the *declared* spec so the RC
                entity's ±10 kW ceiling does not clip it).
            force: Forwarded to the register path; dispatch writes are always
                unconditional at the register level. Also selects whether this
                write is *commanded* (mode change, reconcile, verify retry) and
                so gets the one-shot re-confirm, as opposed to a routine hold.
        """
        dispatch_mode = _DISPATCH_MODES.get(mode) if self._supported() else None
        if dispatch_mode is None:
            await self._release()
            await self._base.apply_mode(mode, spec, force=force)
            return
        watts = self._dispatch_watts(mode)
        payload = {
            "mode": dispatch_mode,
            "power_watts": watts,
            "failsafe_minutes": _FAILSAFE_MINUTES,
        }
        await self._call(SERVICE_DISPATCH, payload)
        if force:
            self._schedule_reconfirm(payload)
        # 44106 is signed per the block's own convention: + export, − import.
        signed = watts if mode is StorageMode.Discharge else -watts
        self._commanded = (_MODE_PCC_TARGET, signed)
        # The base driver still owns the mode's register composition (43110
        # bitmask, currents, export cap). Dispatch steers power within it; it
        # does not replace the operating mode the inverter is in.
        await self._base.apply_mode(mode, self._without_rc(spec), force=force)

    async def hold(self, mode: StorageMode, spec: StorageModeSpec) -> None:
        """Re-push the dispatch block so its failsafe never runs out.

        The failsafe is the deadman, and this is what refreshes it: at
        ``_FAILSAFE_MINUTES`` against a ~5-min cycle there is 4× headroom, so
        unlike the RC path there is no driver-side timer. Passive modes fall
        through to the base driver's hold.

        Args:
            mode: The mode still being held.
            spec: Effective register spec for ``mode``.
        """
        if mode not in _DISPATCH_MODES or not self._supported():
            await self._base.hold(mode, spec)
            return
        await self.apply_mode(mode, spec, force=False)

    def shutdown(self) -> None:
        """Tear down the wrapped driver and drop any pending re-confirm.

        Dispatch itself is deliberately **not** released here: an unload is not
        an instruction to change what the inverter is doing, and the failsafe
        already bounds how long a forgotten dispatch can persist. The pending
        re-confirm *is* cancelled — it would otherwise fire against a
        torn-down entry and issue a ghost write.
        """
        self._cancel_reconfirm()
        self._base.shutdown()

    # --- Control surface ------------------------------------------------- #

    def control_surface(self, commanded: StorageMode | None) -> list[ControlRow]:
        """Return the register rows plus the dispatch rows for a forced mode.

        For a forced mode the dispatch rows carry the targets and the register
        rows stay informational — which is what makes the verify loop check the
        control that is actually holding the power level. For any other mode the
        surface is the base driver's, plus a dispatch-active row targeting 0 so a
        dispatch sunSale failed to release shows up as drift rather than silence.

        Args:
            commanded: The mode whose targets to compare against, or ``None``.

        Returns:
            Canonical rows in write order, dispatch rows last.
        """
        if commanded is not None and not self._supported():
            # Fallback path: the surface is purely the register driver's.
            return list(self._base.control_surface(commanded))
        if commanded in _DISPATCH_MODES:
            # Compare against what was actually written: the RC-free spec.
            # Using the mode's own spec would report the stood-down RC rows as a
            # permanent mismatch that nothing resolves.
            base_spec = self._base.effective_spec(commanded)
            rows = list(self._base.control_surface_for_spec(
                self._without_rc(base_spec) if base_spec is not None else None,
            ))
        else:
            rows = list(self._base.control_surface(commanded))
        if commanded is None:
            return rows
        if commanded not in _DISPATCH_MODES:
            rows.append(self._row(
                "dispatch_active", "Dispatch active (44100)", 0, "dispatch_active",
            ))
            return rows
        target = self._commanded or (
            _MODE_PCC_TARGET,
            self._dispatch_watts(commanded)
            * (1 if commanded is StorageMode.Discharge else -1),
        )
        rows.extend([
            self._row("dispatch_active", "Dispatch active (44100)", 1,
                      "dispatch_active"),
            self._row("dispatch_control_mode", "Dispatch mode (44105)", target[0],
                      "dispatch_control_mode"),
            self._row("dispatch_power_target", "Dispatch power (44106)", target[1],
                      "dispatch_power_target", tolerance=100.0),
            self._row("dispatch_failsafe", "Dispatch failsafe (44101)",
                      _FAILSAFE_MINUTES, "dispatch_failsafe_interval"),
        ])
        return rows

    # --- Internals -------------------------------------------------------- #

    @staticmethod
    def _without_rc(spec: StorageModeSpec) -> StorageModeSpec:
        """Return ``spec`` with the RC setpoint zeroed.

        Two mechanisms must never drive power at once. Handing the base driver
        the mode's own spec would have it engage the RC function — selector,
        timeout, setpoint — *and* start its 3-minute heartbeat, alongside the
        dispatch block already holding a target: two controllers fighting over
        the same inverter, with the RC one expiring after ~5 minutes.

        Zeroing the setpoint takes ``apply_mode``'s release branch instead, so
        the RC function is explicitly stood down (setpoint 0, then selector OFF)
        and no heartbeat arms. The rest of the composition — bitmask, currents,
        export cap — applies unchanged.

        Args:
            spec: The mode's effective spec.

        Returns:
            The same spec with ``rc_setpoint_w`` set to 0.
        """
        return replace(spec, rc_setpoint_w=0)

    def _dispatch_watts(self, mode: StorageMode) -> int:
        """Return the magnitude in watts to command for ``mode``.

        The magnitude escapes exactly **one** ceiling and no others: the RC
        ``number`` entity's declared ±10 kW, which bounds a register this path
        does not write. Everything else that limits the install is still a real
        limit, so the base is the *declared* spec (the inverter rating) reduced
        by the configured export cap.

        **The export cap must be applied here, by us.** On the RC path register
        43074 constrained export, and it still reads back correct — but
        ``solis_dispatch`` writes ``0xFFFF`` ("no system caps") to the dispatch
        block's own import/export limit registers (44103/44104), so a PCC power
        target *overrides* 43074 rather than being clipped by it. Taking the
        declared magnitude alone therefore exported 13.7 kW against a
        configured 10 kW cap on the reference install (2026-08-05 02:45), with
        every register row still reporting ``match``.

        Only the export leg is applied, and only when discharging: it is a cap
        on grid *export*, so it says nothing about GridCharge's import, whose
        magnitude is already the battery's own charge limit. The battery
        discharge leg is deliberately not applied — commanding above what the
        battery can deliver is harmless (the inverter simply delivers less, and
        the planner already plans against ``capability()``), whereas exceeding
        an export cap is a grid-compliance question.

        Args:
            mode: A forced mode present in ``_DISPATCH_MODES``.

        Returns:
            Non-negative power in watts; ``0`` when the mode has no spec.
        """
        declared = self._base.declared_spec(mode)
        if declared is None:
            return 0
        watts = abs(int(declared.rc_setpoint_w))
        if mode is not StorageMode.Discharge:
            return watts
        effective = self._base.effective_spec(mode)
        cap = effective.export_limit_w if effective is not None else None
        if cap is None:
            return watts
        return min(watts, int(cap))

    def _schedule_reconfirm(self, payload: dict[str, Any]) -> None:
        """Arm a single re-write of ``payload`` ``_RECONFIRM_DELAY_S`` from now.

        **The first mode+target write after an enable edge is not reliable.**
        Writing 44100=1 puts the inverter into the dispatch state, and entering
        that state initialises 44105/44106 to ``1``/``0``; ``solis_dispatch``
        sends mode+target as a second register write immediately after the
        enable, so whether it survives is a race against the firmware's own
        initialisation. On the reference install roughly 40 % of commanded
        discharges lost it: dispatch read back *running* with a zero power
        target, exporting nothing, until the next holding tick happened to
        re-push the same block 5 minutes later (measured 2026-08-07…13; caught
        directly on 2026-08-12 05:03, where a poll landed between the two and
        read ``active=1, mode=1, target=0``).

        A second write once dispatch is already running has no enable edge to
        race and has always stuck, so this re-confirm — not a readback check —
        is the repair. The readback cannot detect the loss anyway: the dispatch
        sensors are ``solis_modbus``'s write-through cache, so they echo what was
        commanded and the verify loop reads its own optimistic write back as
        ``match`` (the real value only surfaces on the ~10-min poll).

        Only *commanded* writes arm it (``force=True`` — mode change, reconcile,
        verify retry). A routine hold re-pushes an already-running dispatch,
        which is the safe case by construction.

        Args:
            payload: The same ``solis_dispatch`` payload that was just sent.
        """
        self._cancel_reconfirm()
        self._reconfirm_payload = dict(payload)
        self._reconfirm_cancel = async_call_later(
            self._hass, _RECONFIRM_DELAY_S, self._on_reconfirm,
        )

    async def _on_reconfirm(self, _now: datetime) -> None:
        """Re-send the pending dispatch payload once.

        No-op when dispatch was released in the meantime — re-arming a mode
        sunSale has since stood down is precisely the stale write ``_release``
        exists to prevent.

        Args:
            _now: Fire time (unused).
        """
        self._reconfirm_cancel = None
        payload = self._reconfirm_payload
        self._reconfirm_payload = None
        if payload is None or self._commanded is None:
            return
        _LOGGER.debug("solis_dispatch: re-confirming %s", payload)
        await self._call(SERVICE_DISPATCH, payload)

    def _cancel_reconfirm(self) -> None:
        """Drop any pending re-confirm, so at most one is ever in flight."""
        if self._reconfirm_cancel is not None:
            try:
                self._reconfirm_cancel()
            except Exception:  # noqa: BLE001 — cancel must never raise
                _LOGGER.debug(
                    "solis_dispatch: re-confirm cancel raised — ignoring",
                    exc_info=True,
                )
            self._reconfirm_cancel = None
        self._reconfirm_payload = None

    async def _release(self) -> None:
        """Stop Remote Dispatch, handing control back to the register path."""
        self._cancel_reconfirm()
        if self._commanded is None:
            return
        self._commanded = None
        await self._call(SERVICE_DISPATCH_STOP, {})

    async def _call(self, service: str, data: dict[str, Any]) -> None:
        """Call one ``solis_modbus`` service, logging (not raising) on failure.

        Mirrors ``EntityActuator._call_service``: a mode is a sequence of
        actions and a single rejected call must not abort the ones after it —
        the verify loop is the designed detector for an action that did not
        take.

        Args:
            service: Service name within the ``solis_modbus`` domain.
            data: Service payload.
        """
        _LOGGER.debug("solis_dispatch: %s(%s)", service, data)
        try:
            await self._hass.services.async_call(_DOMAIN, service, data, blocking=True)
        except Exception:  # noqa: BLE001 — one bad call must not abort the sequence
            _LOGGER.error(
                "solis_dispatch: %s.%s(%s) failed; continuing (the verify loop "
                "will flag the mismatch)", _DOMAIN, service, data, exc_info=True,
            )

    def _row(
        self,
        name: str,
        label: str,
        desired: Any,
        role: str,
        tolerance: float = 0.5,
    ) -> ControlRow:
        """Build one dispatch control row from its readback sensor.

        Args:
            name: Stable machine key for the row.
            label: Panel label.
            desired: The value this dispatch commands.
            role: ``entity_ids`` role of the readback sensor.
            tolerance: Absolute slack for the comparison — the power target is
                written in 10 W units through a service that rounds, and the
                inverter reports what it settled on, so it gets coarse slack.

        Returns:
            A :class:`ControlRow` whose ``status`` is ``unknown`` when the
            sensor is unmapped or unreadable.
        """
        observed = self._read(role)
        status: ControlStatus
        if observed is None:
            status = "unknown"
        elif abs(observed - float(desired)) <= tolerance:
            status = "match"
        else:
            status = "mismatch"
        return ControlRow(
            name=name, label=label, desired=desired, observed=observed, status=status,
        )

    def _read(self, role: str) -> float | None:
        """Return a dispatch readback sensor's value, or ``None`` when unreadable."""
        entity_id = self._entity_ids.get(role, "")
        if not entity_id:
            return None
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None


def dispatch_supported(
    hass: HomeAssistant, entity_ids: dict[str, str] | None
) -> bool:
    """Return whether this install can drive Remote Dispatch.

    Both halves must hold: ``solis_modbus`` must be new enough to expose the
    service, and the inverter must advertise the capability on input register
    34502. Either missing means the caller keeps the RC register path — which is
    the point of gating rather than assuming.

    Args:
        hass: Home Assistant instance.
        entity_ids: Role→entity-id map; ``dispatch_capability`` is the sensor
            mirroring register 34502.

    Returns:
        ``True`` when dispatch may be used.
    """
    if not hass.services.has_service(_DOMAIN, SERVICE_DISPATCH):
        _LOGGER.debug(
            "solis_dispatch: %s.%s not registered — staying on the RC path",
            _DOMAIN, SERVICE_DISPATCH,
        )
        return False
    entity_id = (entity_ids or {}).get("dispatch_capability", "")
    if not entity_id:
        _LOGGER.debug(
            "solis_dispatch: no dispatch-capability sensor resolved — "
            "staying on the RC path",
        )
        return False
    state = hass.states.get(entity_id)
    try:
        capable = state is not None and int(float(state.state)) == DISPATCH_CAPABLE_MAGIC
    except (TypeError, ValueError):
        capable = False
    if not capable:
        _LOGGER.info(
            "solis_dispatch: inverter does not advertise Remote Dispatch "
            "(%s reads %s, expected 0x%X) — staying on the RC path",
            entity_id, getattr(state, "state", None), DISPATCH_CAPABLE_MAGIC,
        )
    return capable
