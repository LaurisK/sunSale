#!/usr/bin/env python3
"""Push current branch, force HACS to redownload sunSale, restart HA.

Pipeline:
  1. git push (current branch -> origin)
  2. POST homeassistant.update_entity for update.sunsale_update so HACS
     re-queries GitHub for the latest commit SHA on the tracked branch.
  3. Poll update.sunsale_update until latest_version matches the new HEAD.
  4. POST update.install to make HACS download the new commit.
  5. Poll until installed_version matches HEAD and in_progress clears.
  6. POST homeassistant.restart.
  7. Poll /api/ until HA is back, then poll /api/sun_sale/debug until
     the integration is loaded again.

Usage:
    python tools/deploy.py
    HA_URL=http://host:port HA_TOKEN=... python tools/deploy.py
    python tools/deploy.py --no-push     # skip git push (already pushed)
    python tools/deploy.py --skip-restart
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

# Reuse the shared credential loader from the checks package (CLI arg > env >
# tools/secrets.json). ``tools/`` is this file's own directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from checks.credentials import CredentialsError, resolve_credentials  # noqa: E402

UPDATE_ENTITY = "update.sunsale_update"


class HA:
    """Minimal token-authenticated HA REST client for the deploy script."""

    def __init__(self, base_url: str, token: str, timeout: float = 15.0) -> None:
        """Store the HA base URL, long-lived access token, and request timeout.

        Args:
            base_url: Root URL of the HA instance (trailing slash trimmed).
            token: Long-lived access token used as the bearer credential.
            timeout: Per-request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None) -> Any:
        """Issue a JSON HTTP request to the HA API and return the parsed payload.

        Args:
            method: HTTP method (e.g. ``"GET"`` / ``"POST"``).
            path: API path beginning with ``/api``.
            body: Optional dict serialised as the request JSON body.

        Returns:
            Parsed JSON payload, or ``None`` when the response is empty.
        """
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None

    def state(self, entity_id: str) -> dict | None:
        """Return the HA state dict for ``entity_id``, or ``None`` on HTTP 404.

        Args:
            entity_id: HA entity ID (e.g. ``update.sunsale_update``).

        Returns:
            State dict, or ``None`` when the entity does not exist.
        """
        try:
            return self._req("GET", f"/api/states/{entity_id}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def call(self, domain: str, service: str, data: dict) -> Any:
        """Invoke a HA service and return its JSON response payload.

        Args:
            domain: Service domain (e.g. ``"homeassistant"``).
            service: Service name (e.g. ``"restart"``).
            data: Service call payload.

        Returns:
            Response body as returned by HA, parsed from JSON.
        """
        return self._req("POST", f"/api/services/{domain}/{service}", data)

    def alive(self) -> bool:
        """Return ``True`` when the HA API responds, ``False`` on any network error."""
        try:
            self._req("GET", "/api/")
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            return False


def sh(cmd: list[str]) -> str:
    """Run a shell command and return its trimmed stdout.

    Args:
        cmd: Argv list passed straight to ``subprocess.run``.

    Returns:
        Stripped stdout text. Raises ``CalledProcessError`` on non-zero exit.
    """
    out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def git_head_short() -> str:
    """Return the 7-character abbreviated SHA of HEAD."""
    return sh(["git", "rev-parse", "--short=7", "HEAD"])


def git_push() -> None:
    """Push the currently checked-out branch to ``origin``."""
    branch = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    print(f"[git] push {branch} -> origin")
    subprocess.run(["git", "push", "origin", branch], check=True)


def wait_until(
    desc: str,
    cond: callable,
    timeout_s: float,
    interval_s: float = 3.0,
) -> bool:
    """Poll ``cond`` until it returns truthy or ``timeout_s`` elapses.

    Exceptions raised by ``cond`` are caught and logged so transient HTTP
    failures during HA restart don't abort the wait.

    Args:
        desc: Human-readable description printed alongside progress logs.
        cond: Zero-argument predicate; returning truthy ends the wait.
        timeout_s: Wall-clock budget in seconds.
        interval_s: Sleep between polls.

    Returns:
        ``True`` when ``cond`` succeeded inside the budget, ``False`` on timeout.
    """
    print(f"[wait] {desc} (timeout {int(timeout_s)}s)")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if cond():
                return True
        except Exception as exc:  # noqa: BLE001
            print(f"  poll error: {exc}")
        time.sleep(interval_s)
    return False


def main(argv: list[str] | None = None) -> int:
    """Run the full push → HACS update → restart deploy pipeline.

    Args:
        argv: Optional command-line arguments; defaults to ``sys.argv[1:]``.

    Returns:
        Exit code: 0 on success, 2–5 on the specific phase that failed.
    """
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=None, help="HA base URL (else HA_URL env / secrets.json)")
    p.add_argument("--token", default=None, help="HA token (else HA_TOKEN env / secrets.json)")
    p.add_argument("--no-push", action="store_true", help="skip git push")
    p.add_argument("--skip-restart", action="store_true", help="redownload only, no HA restart")
    p.add_argument("--refresh-timeout", type=int, default=180, help="seconds to wait for HACS to see new SHA")
    p.add_argument("--install-timeout", type=int, default=180, help="seconds to wait for redownload")
    p.add_argument("--restart-timeout", type=int, default=240, help="seconds to wait for HA to come back")
    args = p.parse_args(argv)

    try:
        url, token = resolve_credentials(args.url, args.token)
    except CredentialsError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    ha = HA(url, token)

    if not args.no_push:
        git_push()
    head = git_head_short()
    print(f"[git] HEAD = {head}")

    print(f"[ha] poking {UPDATE_ENTITY} -> homeassistant.update_entity")
    ha.call("homeassistant", "update_entity", {"entity_id": UPDATE_ENTITY})

    def latest_is_head() -> bool:
        """Return True when HACS's latest_version equals the local HEAD SHA."""
        st = ha.state(UPDATE_ENTITY)
        if not st:
            return False
        latest = (st.get("attributes") or {}).get("latest_version")
        print(f"  latest_version={latest}")
        return latest == head

    if not wait_until(f"HACS sees latest_version={head}", latest_is_head, args.refresh_timeout):
        print(f"[error] HACS never reported latest_version={head}", file=sys.stderr)
        return 2

    st = ha.state(UPDATE_ENTITY) or {}
    installed = (st.get("attributes") or {}).get("installed_version")
    if installed == head:
        print(f"[ha] installed_version already {head} — nothing to redownload")
    else:
        print(f"[ha] update.install ({installed} -> {head})")
        ha.call("update", "install", {"entity_id": UPDATE_ENTITY})

        def installed_is_head() -> bool:
            """Return True when HACS finished installing the local HEAD SHA."""
            s = ha.state(UPDATE_ENTITY)
            if not s:
                return False
            attrs = s.get("attributes") or {}
            inst = attrs.get("installed_version")
            in_progress = attrs.get("in_progress")
            print(f"  installed_version={inst} in_progress={in_progress}")
            return inst == head and not in_progress

        if not wait_until(f"installed_version={head}", installed_is_head, args.install_timeout):
            print(f"[error] redownload did not complete to {head}", file=sys.stderr)
            return 3

    if args.skip_restart:
        print("[done] redownload complete, restart skipped")
        return 0

    print("[ha] homeassistant.restart")
    try:
        ha.call("homeassistant", "restart", {})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        # Restart usually drops the connection mid-request; that's expected.
        print(f"  (request dropped as expected: {e})")

    # Wait for the API to disappear briefly, then come back.
    time.sleep(5)

    if not wait_until("HA API responding", ha.alive, args.restart_timeout, interval_s=4.0):
        print("[error] HA did not come back online", file=sys.stderr)
        return 4

    def integration_loaded() -> bool:
        """Return True when the sunSale debug endpoint serves a non-empty list."""
        try:
            payload = ha._req("GET", "/api/sun_sale/debug")  # noqa: SLF001
        except urllib.error.HTTPError as e:
            print(f"  /api/sun_sale/debug -> HTTP {e.code}")
            return False
        ok = isinstance(payload, list) and len(payload) > 0
        print(f"  /api/sun_sale/debug -> {len(payload) if isinstance(payload, list) else '?'} entries")
        return ok

    if not wait_until("sunSale integration loaded", integration_loaded, 120, interval_s=4.0):
        print("[warn] HA back up but sunSale debug endpoint not responding yet", file=sys.stderr)
        return 5

    print(f"[done] deployed {head} and HA restarted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
