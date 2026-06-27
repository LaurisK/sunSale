"""Inbound telemetry seam — platform-neutral read side (first slice).

This package is the read-side mirror of the outbound ``InverterDriver`` seam.
See ``docs/telemetry_seam.md`` for the full design. This slice ships only the
two **pure** layers — the signal vocabulary (:mod:`binding`) and the sign/unit
transform (:mod:`codec`) — with no Home Assistant coupling and no wiring into
the observers yet. The live ``TelemetryReader`` and the entity-resolution
adapter come in later slices.
"""
from __future__ import annotations

from .binding import SignalBinding, SignalRole, TelemetrySignal
from .codec import GenericCodec, SolisCodec, TelemetryCodec, signed_polarity
from .reader import HaTelemetryReader, TelemetryReader, resolve_bindings

__all__ = [
    "SignalBinding",
    "SignalRole",
    "TelemetrySignal",
    "TelemetryCodec",
    "SolisCodec",
    "GenericCodec",
    "signed_polarity",
    "TelemetryReader",
    "HaTelemetryReader",
    "resolve_bindings",
]
