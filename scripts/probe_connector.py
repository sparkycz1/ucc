#!/usr/bin/env python3
"""Probe what the UniFi Site Manager API + connector proxy can actually do.

Read-only by default. Stdlib only, so it runs anywhere:

    UNIFI_API_KEY=... python3 scripts/probe_connector.py
    UNIFI_API_KEY=... python3 scripts/probe_connector.py --host-name <console name>
    UNIFI_API_KEY=... python3 scripts/probe_connector.py --host <hostId> \\
        --upgrade-device <mac> --site default --yes-really-upgrade

The only write it can perform is a single device firmware upgrade
(legacy ``cmd/devmgr`` ``upgrade``), and only when both ``--upgrade-device``
and ``--yes-really-upgrade`` are passed. Use a test device.

Output: a summary table on stdout plus full JSON responses in
``probe_output/`` (they contain your inventory, don't share them blindly).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE = "https://api.ui.com"
OUT = Path("probe_output")


def call(key: str, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(  # noqa: S310 - always https://api.ui.com + a fixed path
        BASE + path,
        data=data,
        method=method,
        headers={
            "X-API-Key": key,
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:  # noqa: S310 - see above
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except (urllib.error.URLError, TimeoutError) as exc:
        return 0, {"error": str(exc)}
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, {"raw": raw[:2000].decode(errors="replace")}


def save(name: str, payload: Any) -> None:
    OUT.mkdir(exist_ok=True)
    safe = name.strip("/").replace("/", "_").replace(":", "-") or "root"
    (OUT / f"{safe}.json").write_text(json.dumps(payload, indent=2))


results: list[tuple[str, str, int, str]] = []


def probe(key: str, method: str, path: str, note: str, body: dict[str, Any] | None = None) -> Any:
    status, payload = call(key, method, path, body)
    save(f"{method}_{path}", {"status": status, "body": payload})
    results.append((method, path, status, note))
    return payload if 200 <= status < 300 else None


def items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return payload["data"]
    return payload if isinstance(payload, list) else []


def _host_name(h: dict[str, Any]) -> str:
    rs = h.get("reportedState") or {}
    return str(rs.get("name") or rs.get("hostname") or "")


def _print_pending(host: dict[str, Any], devices_payload: Any) -> set[str]:
    """What is waiting to be updated on this console, from Site Manager's
    own data. Returns the names of the installed applications."""
    rs = host.get("reportedState") or {}
    hw = rs.get("hardware") or {}
    fw_update = rs.get("firmwareUpdate") or {}
    print(f"\n=== {_host_name(host)} ({hw.get('name') or hw.get('shortname')})")
    print(
        f"  UniFi OS {rs.get('version')}  firmware {hw.get('firmwareVersion')}"
        f"  -> update: {fw_update.get('latestAvailableVersion') or 'none reported'}"
    )
    print(f"  UniFi auto-update: {json.dumps(rs.get('autoUpdate'))}")
    installed: set[str] = set()
    for ctl in rs.get("controllers") or []:
        if not isinstance(ctl, dict) or ctl.get("isInstalled") is False:
            continue
        installed.add(str(ctl.get("name")))
        print(
            f"  app {ctl.get('name')}: {ctl.get('version')}"
            f"  -> update: {ctl.get('updateAvailable') or 'none'}"
            f"  updatable={ctl.get('updatable')}  state={ctl.get('state')}"
        )
    for entry in items(devices_payload):
        if entry.get("hostId") != host.get("id"):
            continue
        for dev in entry.get("devices") or []:
            print(
                f"  device {dev.get('name')} [{dev.get('shortname') or dev.get('model')}]"
                f" mac={dev.get('mac')} {dev.get('version') or '?'}"
                f" status={dev.get('status')} firmwareStatus={dev.get('firmwareStatus')}"
                f" -> {dev.get('updateAvailable') or '-'}"
            )
    return installed


def main() -> int:
    global OUT
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = ap.add_mutually_exclusive_group()
    target.add_argument("--host", help="hostId (console) to probe via connector")
    target.add_argument("--host-name", help="console name as shown in Site Manager")
    ap.add_argument("--site", default="default", help="legacy Network site name")
    ap.add_argument("--upgrade-device", metavar="MAC", help="Network device to upgrade")
    ap.add_argument("--yes-really-upgrade", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("UNIFI_API_KEY")
    if not key:
        print("Set UNIFI_API_KEY", file=sys.stderr)
        return 2

    # --- Site Manager read endpoints -------------------------------------
    hosts = items(probe(key, "GET", "/v1/hosts", "consoles"))
    probe(key, "GET", "/v1/sites", "sites")
    devices_payload = probe(key, "GET", "/v1/devices", "devices + firmware status")

    print(f"\n{len(hosts)} host(s):")
    for h in hosts:
        rs = h.get("reportedState") or {}
        print(
            f"  {h.get('id')}  {_host_name(h)}  type={h.get('type')}  UniFi OS {rs.get('version')}"
        )

    if args.host_name:
        wanted = args.host_name.strip().lower()
        matches = [h for h in hosts if _host_name(h).lower() == wanted]
        if not matches:
            print(f"\nNo console named {args.host_name!r} for this key.")
            return finish()
        host_obj = matches[0]
    elif args.host:
        host_obj = next((h for h in hosts if h.get("id") == args.host), {"id": args.host})
    elif hosts:
        host_obj = hosts[0]
    else:
        print("No host to probe via connector.")
        return finish()
    host = str(host_obj["id"])
    # One folder per console, so runs against different consoles don't mix.
    OUT = OUT / (_host_name(host_obj) or host.split(":")[0][:12])
    installed = _print_pending(host_obj, devices_payload)

    c = f"/v1/connector/consoles/{host}"
    # UniFi OS itself (local paths outside /proxy/) — 403 with a read-only
    # key so far; a write-enabled key may differ.
    probe(key, "GET", f"{c}/api/firmware/update", "UniFi OS fw update info")
    probe(key, "GET", f"{c}/api/system", "UniFi OS system")
    probe(key, "GET", f"{c}/api/controllers", "UniFi OS apps")
    probe(key, "GET", f"{c}/proxy/api/firmware/update", "UniFi OS fw update (proxy/ form)")

    if "network" in installed or not installed:
        probe(key, "GET", f"{c}/proxy/network/integration/v1/info", "Network integration info")
        sites = items(probe(key, "GET", f"{c}/proxy/network/integration/v1/sites", "Network sites"))
        for s in sites[:3]:
            probe(
                key,
                "GET",
                f"{c}/proxy/network/integration/v1/sites/{s.get('id')}/devices",
                "Network integration devices",
            )
        probe(key, "GET", f"{c}/proxy/network/api/self/sites", "Network legacy sites")
        devs = items(
            probe(key, "GET", f"{c}/proxy/network/api/s/{args.site}/stat/device", "legacy devices")
        )
        upg = [d for d in devs if d.get("upgradable")]
        print(f"\nNetwork legacy stat/device: {len(devs)} device(s), {len(upg)} upgradable")
        for d in upg:
            print(
                f"  {d.get('mac')} {d.get('name')} {d.get('version')}"
                f" -> {d.get('upgrade_to_firmware')}"
            )

    if "protect" in installed:
        probe(key, "GET", f"{c}/proxy/protect/integration/v1/meta/info", "Protect version")
        cams = items(probe(key, "GET", f"{c}/proxy/protect/integration/v1/cameras", "cameras"))
        print(f"\nProtect integration: {len(cams)} camera(s)")
        for cam in cams[:50]:
            print(
                f"  {cam.get('name')} [{cam.get('modelKey') or cam.get('type')}]"
                f" mac={cam.get('mac')} state={cam.get('state')}"
                f" fw={cam.get('firmwareVersion')}"
            )

    # --- Optional single write (Network device firmware only) -------------
    if args.upgrade_device:
        if not args.yes_really_upgrade:
            print("\n--upgrade-device given without --yes-really-upgrade: skipped.")
        else:
            probe(
                key,
                "POST",
                f"{c}/proxy/network/api/s/{args.site}/cmd/devmgr",
                f"WRITE: upgrade {args.upgrade_device}",
                {"cmd": "upgrade", "mac": args.upgrade_device.lower()},
            )
    return finish()


def finish() -> int:
    print("\nstatus  method  path  (note)")
    for method, path, status, note in results:
        print(f"{status:>6}  {method:<5} {path}  ({note})")
    print(f"\nFull responses in {OUT.resolve()}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
