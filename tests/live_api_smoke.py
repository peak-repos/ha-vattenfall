#!/usr/bin/env python3
"""Live smoke test for the Vattenfall supplier (Mina sidor) API client.

Usage::

    VF_CUSTOMER_ID=2001234567 VF_PASSWORD='hunter2' \
        python3 tests/live_api_smoke.py

Env vars (all optional; prompted interactively if omitted):

- ``VF_CUSTOMER_ID`` / ``VF_PASSWORD`` -- login credentials
- ``VF_PREMISE_ID``                    -- pin to a specific premise
- ``VF_DEBUG=1``                       -- verbose ``httpx`` request logging

Only depends on ``httpx`` (a runtime dep of the integration). It does NOT
require Home Assistant to be installed.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Load const.py + api.py inside a synthetic ``custom_components.vattenfall``
# package so that ``api.py``'s ``from .const import ...`` works WITHOUT pulling
# in the real package ``__init__.py`` (which imports voluptuous, HA, etc.).
import importlib.util  # noqa: E402
import types  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = REPO_ROOT / "custom_components" / "vattenfall"

# Minimal stub for the one Home Assistant symbol const.py needs.
if "homeassistant" not in sys.modules:
    sys.modules["homeassistant"] = types.ModuleType("homeassistant")
if "homeassistant.const" not in sys.modules:
    ha_const = types.ModuleType("homeassistant.const")
    ha_const.CONF_PASSWORD = "password"
    sys.modules["homeassistant.const"] = ha_const

# Synthetic empty parent packages so relative imports resolve.
for pkg_name, pkg_path in (
    ("custom_components", REPO_ROOT / "custom_components"),
    ("custom_components.vattenfall", PKG_DIR),
):
    if pkg_name not in sys.modules:
        mod = types.ModuleType(pkg_name)
        mod.__path__ = [str(pkg_path)]
        sys.modules[pkg_name] = mod


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, PKG_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


_load("custom_components.vattenfall.const", "const.py")
api = _load("custom_components.vattenfall.api", "api.py")

VattenfallApiClient = api.VattenfallApiClient
VattenfallApiError = api.VattenfallApiError
VattenfallAuthError = api.VattenfallAuthError


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Quiet down httpx unless explicitly verbose.
    if not verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("hpack").setLevel(logging.WARNING)


async def _run(customer_id: str, password: str, premise_id: str | None) -> int:
    client = VattenfallApiClient(
        customer_id=customer_id, password=password, premise_id=premise_id
    )
    try:
        print("→ Authenticating...")
        await client.async_authenticate(force=True)
        print("  ✓ logged in")

        print("→ Listing premises...")
        premises = await client.async_list_premises()
        if not premises:
            print("  ✗ No premises returned. Raw payload may have a different shape.")
            return 1
        for p in premises:
            print(f"  • {p.premise_id}  {p.address or ''}")

        target = premise_id or premises[0].premise_id
        print(f"→ Using premise {target}")

        print("→ Fetching measurement-ranges...")
        ranges = await client.async_get_measurement_ranges(premise_id=target)
        for r in ranges:
            print(
                f"  • {r.get('resolution'):<11} "
                f"first={r.get('firstAvailableMeasurementDate')} "
                f"last={r.get('lastAvailableMeasurementDate')}"
            )

        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=2)
        print(f"→ Fetching daily consumption {start}..{end}...")
        daily = await client.async_get_daily_consumption(start, end, premise_id=target)
        for p in daily:
            print(f"  • {p.date}  {p.value_kwh:.3f} kWh")

        print(f"→ Fetching hourly consumption for {end}...")
        hourly = await client.async_get_hourly_consumption(end, end, premise_id=target)
        if hourly:
            print(f"  got {len(hourly)} points; first 3 + last 1:")
            for p in hourly[:3]:
                print(f"    {p.date_time}  {p.value_kwh:.3f} kWh")
            print("    ...")
            print(f"    {hourly[-1].date_time}  {hourly[-1].value_kwh:.3f} kWh")
        else:
            print("  ✗ no hourly points returned")
        return 0
    except VattenfallAuthError as err:
        print(f"AUTH FAILED: {err}", file=sys.stderr)
        return 2
    except VattenfallApiError as err:
        print(f"API ERROR: {err}", file=sys.stderr)
        return 3
    finally:
        await client.async_close()


def main() -> int:
    verbose = os.environ.get("VF_DEBUG") == "1"
    _setup_logging(verbose)

    customer_id = os.environ.get("VF_CUSTOMER_ID") or input("Customer ID: ").strip()
    password = os.environ.get("VF_PASSWORD") or getpass.getpass("Password: ")
    premise_id = os.environ.get("VF_PREMISE_ID") or None

    if not customer_id or not password:
        print("Missing customer id or password", file=sys.stderr)
        return 64

    return asyncio.run(_run(customer_id, password, premise_id))


if __name__ == "__main__":
    raise SystemExit(main())
