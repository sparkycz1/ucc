#!/usr/bin/env python3
"""Probe what the UniFi Site Manager API + connector proxy can actually do.

Read-only by default. Stdlib only, so it runs anywhere:

    UNIFI_API_KEY=... python3 scripts/probe_connector.py
    UNIFI_API_KEY=... python3 scripts/probe_connector.py --host <hostId>
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", help="hostId (console) to probe via connector")
    ap.add_argument("--site", default="default", help="legacy site name")
    ap.add_argument("--upgrade-device", metavar="MAC")
    ap.add_argument("--yes-really-upgrade", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("UNIFI_API_KEY")
    if not key:
        print("Set UNIFI_API_KEY", file=sys.stderr)
        return 2

    # --- Site Manager read endpoints -------------------------------------
    hosts = items(probe(key, "GET", "/v1/hosts", "consoles"))
    probe(key, "GET", "/v1/sites", "sites")
    probe(key, "GET", "/v1/devices", "devices + firmware status")

    print(f"\n{len(hosts)} host(s):")
    for h in hosts:
        rs = h.get("reportedState") or {}
        print(
            f"  {h.get('id')}  {rs.get('hostname') or rs.get('name')}"
            f"  type={h.get('type')}  version={rs.get('version')}"
            f"  fw={rs.get('firmwareVersion') or rs.get('firmware_version')}"
        )

    host = args.host or (hosts[0]["id"] if hosts else None)
    if not host:
        print("No host to probe via connector.")
        return finish()

    c = f"/v1/connector/consoles/{host}"
    # Spec is ambiguous whether "proxy/" is part of the path -> try both.
    probe(key, "GET", f"{c}/network/integration/v1/info", "integration info (no proxy/)")
    probe(key, "GET", f"{c}/proxy/network/integration/v1/info", "integration info (proxy/)")
    sites = items(probe(key, "GET", f"{c}/proxy/network/integration/v1/sites", "integration sites"))
    for s in sites[:3]:
        probe(
            key,
            "GET",
            f"{c}/proxy/network/integration/v1/sites/{s.get('id')}/devices",
            "integration devices",
        )

    # Legacy Network API (has upgradable / upgrade_to_firmware / cmd/devmgr)
    probe(key, "GET", f"{c}/proxy/network/api/self/sites", "legacy sites")
    probe(
        key,
        "GET",
        f"{c}/proxy/network/api/s/{args.site}/stat/sysinfo",
        "legacy sysinfo (Network app version)",
    )
    devs = items(
        probe(key, "GET", f"{c}/proxy/network/api/s/{args.site}/stat/device", "legacy devices")
    )
    upg = [d for d in devs if d.get("upgradable")]
    print(f"\nlegacy stat/device: {len(devs)} device(s), {len(upg)} upgradable")
    for d in upg:
        print(
            f"  {d.get('mac')} {d.get('name')} {d.get('version')} -> {d.get('upgrade_to_firmware')}"
        )

    # UniFi OS / app update endpoints (local paths outside /proxy/)
    probe(key, "GET", f"{c}/api/firmware/update", "UniFi OS fw update info")
    probe(key, "GET", f"{c}/api/system", "UniFi OS system")
    probe(key, "GET", f"{c}/api/controllers", "UniFi OS apps")
    probe(key, "GET", f"{c}/proxy/protect/integration/v1/meta/info", "Protect version")

    # --- Optional single write ------------------------------------------
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
