# Vattenfall (Mina sidor) Home Assistant integration

Custom integration for Home Assistant that retrieves electricity consumption from a Vattenfall **supply** account (Mina sidor at `vattenfall.se`) and exposes sensors for dashboards, the Energy panel, and automations.

> This is a fork of [`nicklaswallgren/ha-vattenfall`](https://github.com/nicklaswallgren/ha-vattenfall) rebuilt to use the Vattenfall *supplier* APIs at `selfserviceapi.www.vattenfall.se` instead of the Vattenfall *Eldistribution* (grid) APIs. It is intended for customers who buy electricity from Vattenfall regardless of who their grid operator is.

## Features

- Config flow setup from the Home Assistant UI (Customer number + password only)
- Automatic premise discovery (with picker if you have multiple)
- Hourly data updates via coordinator
- Consumption sensors anchored to the **last finalised day** reported by Vattenfall (avoids displaying partial day totals while readings are still uploading):
  - Last finalised day consumption
  - Month-to-date consumption (finalised)
  - Average daily consumption (finalised)
  - Latest reported hour consumption
  - Today's consumption so far (partial)
  - Last finalised day peak hour
- Long-term statistics for the Energy panel (daily + hourly external statistics, kept up to date automatically)
- Backfill service for historical data
- HACS compatible

### "Finalised" vs "today's partial" semantics

Vattenfall's API returns 24 placeholder rows of `0.0 kWh` for any day that has not yet finished uploading from the meter. To avoid sensors that lie, the integration calls `measurement-ranges` first and uses `lastAvailableMeasurementDate` for the `Hourly` resolution as the authoritative cutoff. Everything tagged "finalized" refers to data on or before that cutoff; the **`finalized_date`** attribute on every entity tells you which date the value refers to.

The `Today's consumption so far (partial)` sensor sums whatever hourly readings have already been reported for today and is informational only — do not use it for the Energy panel.

## Installation

Requirements:

- Home Assistant `2024.6.0` or later

### Install with HACS

1. Open HACS → `Integrations` → menu (`⋮`) → `Custom repositories`.
2. Add this repository URL with category `Integration`.
3. Search for `Vattenfall` in HACS and install it.
4. Restart Home Assistant.

### Manual install

1. Copy `custom_components/vattenfall` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.

## Configuration

1. In Home Assistant, go to `Settings` -> `Devices & Services`.
2. Click `Add Integration` and search for `Vattenfall`.
3. Enter:
   - `Customer ID` (the same number you use to sign in at `vattenfall.se/mina-sidor`)
   - `Password`
4. If your account has multiple premises you'll be asked to pick one. Each premise becomes a separate config entry; add the integration again to track another premise.

## Energy dashboard

The live sensors above are **snapshots**, not cumulative meters, so they are not suitable for the Energy panel directly. Instead, point the Energy panel at the **external statistics** that the integration writes to the recorder:

1. **Settings → Dashboards → Energy → Electricity grid → Add consumption → "Use existing statistic"**
2. Pick **`vattenfall:hourly_consumption_<premise_id_lowercase>`** (recommended — hourly resolution).
   - The daily counterpart `vattenfall:daily_consumption_<premise_id_lowercase>` also works but has lower resolution.
3. Save. The panel will start populating from whatever history is in the recorder.

These statistic streams are:
- Updated incrementally on every poll for the most recent finalized day (idempotent overwrites — safe to re-run).
- Backfilled in larger ranges by calling the `vattenfall.backfill` service (see below).

Statistic IDs are stable per premise, so reinstalling the integration on the same premise keeps the existing history.

## Notes

- The integration is configured to update consumption data every hour.
- Runtime dependency `httpx[http2]` is declared in `manifest.json` and installed by Home Assistant.
- Debug logs can include sensitive data (credentials/cookies/tokens). Do not share raw debug output.

## Backfill service

You can backfill historical consumption data from Home Assistant using the custom service:

- Service: `vattenfall.backfill`
- Fields:
  - `start_date` (required, `YYYY-MM-DD`)
  - `end_date` (required, `YYYY-MM-DD`)
  - `mode` (optional: `daily`, `hourly`, `all`, default `all`)
  - `entry_id` (optional: target one specific config entry)

Example call in Developer Tools -> Services:

```yaml
service: vattenfall.backfill
data:
  start_date: "2026-03-01"
  end_date: "2026-03-27"
  mode: all
```

## Development

Install test dependencies:

```bash
python3 -m pip install -r requirements-test.txt
```

Run unit tests:

```bash
python3 -m unittest discover -s tests -v
```

Run the live API smoke test against your real account (requires `httpx[http2]`):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install 'httpx[http2]'
VF_CUSTOMER_ID=200xxxxxxx VF_PASSWORD='...' python tests/live_api_smoke.py
```

Useful environment variables:

- `VF_PREMISE_ID=STH...` — pin to a specific premise instead of auto-picking the first
- `VF_DEBUG=1` — verbose `httpx` request logging
