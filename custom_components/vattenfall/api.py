"""API client for the Vattenfall *supplier* (Mina sidor / sales) self-service API.

This is the customer-facing portal at ``www.vattenfall.se/mina-sidor`` that all
Vattenfall electricity *supply* customers can use, regardless of who their
*grid* (elnät) operator is. The data API lives at
``selfserviceapi.www.vattenfall.se`` and is authenticated purely via session
cookies set during the OAuth login round-trip with the WSO2 IAM tenant
``seb2c/sales`` (the existing ``api.py`` client uses the sibling ``seb2c/dso``
tenant for grid customers).

This module is intentionally self-contained: it only depends on ``httpx`` so
it can be exercised by standalone smoke tests without Home Assistant.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlencode, urljoin

import httpx

from .const import CONF_CUSTOMER_ID, CONF_PASSWORD, CONF_PREMISE_ID

_LOGGER = logging.getLogger(__name__)

# --- Constants ---------------------------------------------------------------

SUPPLIER_API_BASE = "https://selfserviceapi.www.vattenfall.se"
ACCOUNTS_BASE = "https://accounts.vattenfall.se"
WEB_BASE = "https://www.vattenfall.se"

# Static OAuth client registered for the Mina Sidor SPA.
OAUTH_CLIENT_ID = "ALqiYzRolhQdSI3R7e5SxzWdb3Ya"
OAUTH_REDIRECT_URI = f"{WEB_BASE}/iam/se/authcallback"
OAUTH_AUTHORIZE_URL = f"{ACCOUNTS_BASE}/iamng/seb2c/sales/oauth2/authorize"
COMMONAUTH_URL = f"{ACCOUNTS_BASE}/iamng/seb2c/sales/commonauth"
LOGIN_INIT_URL = f"{WEB_BASE}/iam/se/login"

SESSION_NONCE_PREFIX = "sessionNonceCookie-"
TENANT_DOMAIN = "se.b2c"

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

_MAX_SERVER_ERROR_RETRIES = 3
_RETRY_DELAY_S = 2.0


# --- Errors ------------------------------------------------------------------


class VattenfallApiError(Exception):
    """Generic Vattenfall API error."""


class VattenfallAuthError(VattenfallApiError):
    """Authentication-related error."""


# --- Data classes ------------------------------------------------------------


@dataclass
class Premise:
    """A single Vattenfall supplier premise (electricity contract)."""

    premise_id: str
    address: str | None = None
    raw: dict[str, Any] | None = None


@dataclass
class HourlyConsumptionPoint:
    """One hourly consumption datapoint in local (Swedish) time."""

    date_time: str  # ISO-8601, naive local time, e.g. "2026-05-24T13:00:00"
    value_kwh: float


@dataclass
class DailyConsumptionPoint:
    """One daily consumption datapoint."""

    date: str  # YYYY-MM-DD
    value_kwh: float


# --- Client ------------------------------------------------------------------


class VattenfallApiClient:
    """Async client for the Vattenfall supplier-side self-service API.

    Auth flow (mirrors what the SPA does in the browser):

    1. ``GET https://www.vattenfall.se/iam/se/login`` to seed cookies including
       ``.VFState`` (the OAuth ``state`` value bound to the browser session).
    2. ``GET https://accounts.vattenfall.se/iamng/seb2c/sales/oauth2/authorize``
       with ``client_id``, ``redirect_uri`` and the ``state`` from step 1.
       The 302 response sets a ``sessionNonceCookie-<sessionDataKey>`` cookie.
    3. ``POST https://accounts.vattenfall.se/iamng/seb2c/sales/commonauth`` with
       customer id + password + extracted ``sessionDataKey``.
    4. Follow the resulting redirect chain ending at
       ``https://www.vattenfall.se/iam/se/authcallback?code=...&state=...``
       which sets ``.AspNetCore.Identity.Application`` and friends on
       ``www.vattenfall.se``. These cookies are scoped so they are also sent
       to ``selfserviceapi.www.vattenfall.se``.
    """

    def __init__(
        self,
        *,
        customer_id: str | None = None,
        password: str | None = None,
        premise_id: str | None = None,
        hass: Any | None = None,
        config: dict[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """Create the client either from explicit credentials or a HA config dict."""
        if config is not None:
            customer_id = customer_id or config.get(CONF_CUSTOMER_ID)
            password = password or config.get(CONF_PASSWORD)
            premise_id = premise_id or config.get(CONF_PREMISE_ID)
        if not customer_id or not password:
            raise ValueError("customer_id and password are required")
        self._customer_id = customer_id
        self._password = password
        self._premise_id = premise_id
        self._hass = hass
        self._client = client
        self._owns_client = client is None

    @property
    def premise_id(self) -> str | None:
        """The premise this client is bound to, if any."""
        return self._premise_id

    # ---- Lifecycle ---------------------------------------------------------

    @staticmethod
    def _create_httpx_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(http2=True, timeout=30.0)

    async def _async_get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        # Off-load the (synchronous) httpx client construction when running
        # inside Home Assistant so we don't block the event loop.
        if self._hass is not None and hasattr(self._hass, "async_add_executor_job"):
            self._client = await self._hass.async_add_executor_job(self._create_httpx_client)
        else:
            self._client = self._create_httpx_client()
        return self._client

    async def async_close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # ---- Auth --------------------------------------------------------------

    @property
    def _is_authenticated(self) -> bool:
        return self._cookie(".AspNetCore.Identity.Application") is not None

    async def async_authenticate(self, force: bool = False) -> None:
        """Run the full login flow if not already authenticated."""
        if not force and self._is_authenticated:
            return
        try:
            await self._async_login()
        except httpx.HTTPError as err:
            raise VattenfallApiError(f"Network error during authentication: {err}") from err

    async def _async_login(self) -> None:
        client = await self._async_get_client()
        client.cookies.clear()

        # Step 1: seed cookies on www.vattenfall.se (sets .VFState etc.)
        _LOGGER.debug("supplier auth: GET %s", LOGIN_INIT_URL)
        await client.get(
            LOGIN_INIT_URL,
            headers=self._browser_headers(),
            follow_redirects=True,
        )

        vf_state = self._cookie(".VFState", domain_hint="vattenfall.se")
        if not vf_state:
            # If the server didn't set .VFState we still try with a random
            # state; some flows accept this, but most likely auth will fail
            # and we surface that clearly later.
            vf_state = secrets.token_urlsafe(24)
            _LOGGER.debug("supplier auth: no .VFState cookie present, using fresh state")

        # Step 2: OAuth authorize on the IAM tenant.
        authorize_qs = urlencode(
            {
                "client_id": OAUTH_CLIENT_ID,
                "response_type": "code",
                "scope": "openid",
                "redirect_uri": OAUTH_REDIRECT_URI,
                "state": vf_state,
            }
        )
        authorize_url = f"{OAUTH_AUTHORIZE_URL}?{authorize_qs}"
        _LOGGER.debug("supplier auth: GET %s", authorize_url)
        resp = await client.get(
            authorize_url,
            headers=self._browser_headers(referer=f"{WEB_BASE}/"),
            follow_redirects=False,
        )
        if resp.status_code not in (301, 302, 303, 307, 308):
            # The IAM normally responds 302 to the SPA hash URL; anything else
            # likely means the request was rejected.
            raise VattenfallAuthError(
                f"Unexpected status {resp.status_code} from OAuth authorize step"
            )

        session_data_key = self._extract_session_data_key()
        if not session_data_key:
            raise VattenfallAuthError(
                "Could not extract sessionDataKey after OAuth authorize step"
            )

        # Step 3: submit credentials.
        form = {
            "customerId": self._customer_id,
            "password": self._password,
            "auth_method": "customerid_password",
            "tenantDomain": TENANT_DOMAIN,
            "sessionDataKey": session_data_key,
        }
        _LOGGER.debug("supplier auth: POST %s", COMMONAUTH_URL)
        resp = await client.post(
            COMMONAUTH_URL,
            data=form,
            headers=self._browser_headers(
                content_type_form=True,
                origin=ACCOUNTS_BASE,
                referer=f"{ACCOUNTS_BASE}/iamng/seb2c/sales/web/",
            ),
            follow_redirects=False,
        )
        if resp.status_code in (401, 403):
            raise VattenfallAuthError("Invalid Vattenfall supplier credentials")
        if resp.status_code not in (301, 302, 303, 307, 308):
            raise VattenfallAuthError(
                f"Unexpected status {resp.status_code} from commonauth step"
            )

        # Step 4: follow redirect chain through OAuth authorize -> authcallback
        # -> /mina-sidor/. We follow up to 8 redirects manually so we can spot
        # auth failures early (an auth failure typically loops back to the
        # login page rather than reaching authcallback).
        next_url: str | None = resp.headers.get("Location")
        base_url = COMMONAUTH_URL
        reached_callback = False
        for _ in range(8):
            if not next_url:
                break
            absolute = urljoin(base_url, next_url)
            _LOGGER.debug("supplier auth: follow GET %s", absolute)
            resp = await client.get(
                absolute,
                headers=self._browser_headers(),
                follow_redirects=False,
            )
            base_url = absolute
            if "/iam/se/authcallback" in absolute:
                reached_callback = True
            if resp.status_code in (301, 302, 303, 307, 308):
                next_url = resp.headers.get("Location")
                continue
            break

        if not reached_callback:
            raise VattenfallAuthError(
                "Login redirect chain did not reach /iam/se/authcallback "
                "(likely invalid credentials or unexpected IAM response)"
            )
        if not self._is_authenticated:
            raise VattenfallAuthError(
                "Login completed without setting .AspNetCore.Identity.Application cookie"
            )

    # ---- API calls ---------------------------------------------------------

    def _resolve_premise(self, premise_id: str | None) -> str:
        pid = premise_id or self._premise_id
        if not pid:
            raise VattenfallApiError(
                "premise_id is required (pass explicitly or set at construction)"
            )
        return pid

    async def async_list_premises(
        self,
        *,
        include_future_contracts: bool = True,
    ) -> list[Premise]:
        """Return the list of premises (electricity supply contracts) for the user."""
        await self.async_authenticate()
        params = {
            "includeFutureContracts": "true" if include_future_contracts else "false",
            "contractTypeFilter": "",
            "excludeContractTypes": "false",
            "combineMicroProductionPremises": "false",
            "combineBatteryPremises": "false",
        }
        # Replicate the literal double-slash that the SPA uses in case the
        # backend's routing depends on it.
        url = f"{SUPPLIER_API_BASE}//elements/my-premises-select?{urlencode(params)}"
        payload = await self._async_get_json(url, label="my-premises-select")
        return _parse_premises(payload)

    async def async_get_hourly_consumption(
        self,
        start: date,
        end: date,
        *,
        premise_id: str | None = None,
    ) -> list[HourlyConsumptionPoint]:
        """Fetch hourly electricity consumption between ``start`` and ``end`` (inclusive)."""
        pid = self._resolve_premise(premise_id)
        payload = await self._async_with_auth_retry(
            lambda: self._async_fetch_measurement(pid, start, end, "Hourly")
        )
        return _parse_hourly(payload)

    async def async_get_daily_consumption(
        self,
        start: date,
        end: date,
        *,
        premise_id: str | None = None,
    ) -> list[DailyConsumptionPoint]:
        """Fetch daily electricity consumption between ``start`` and ``end`` (inclusive)."""
        pid = self._resolve_premise(premise_id)
        payload = await self._async_with_auth_retry(
            lambda: self._async_fetch_measurement(pid, start, end, "Daily")
        )
        return _parse_daily(payload)

    async def _async_with_auth_retry(self, op):
        """Run ``op()`` once; on auth error reauthenticate and retry exactly once."""
        try:
            return await op()
        except VattenfallAuthError:
            await self.async_authenticate(force=True)
            return await op()

    async def _async_fetch_measurement(
        self,
        premise_id: str,
        start: date,
        end: date,
        resolution: str,
    ) -> dict[str, Any]:
        await self.async_authenticate()
        # The endpoint takes Unix epoch seconds. The browser SPA uses 00:00:00
        # local Swedish time for ``start`` and 23:59:59 for ``end``. We
        # approximate that by using midnight UTC of each date which yields the
        # same Swedish local-day window for the dates the API returns.
        start_epoch = int(datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        end_epoch = int(datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        params = {
            "resolution": resolution,
            "measurementType": "ElectricityConsumption",
            "startDate": str(start_epoch),
            "endDate": str(end_epoch),
        }
        url = (
            f"{SUPPLIER_API_BASE}//elements/my-energy/premises/{premise_id}/measurement?"
            + urlencode(params)
        )
        return await self._async_get_json(url, label=f"measurement-{resolution.lower()}")

    async def async_get_measurement_ranges(
        self, premise_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return the available measurement ranges per resolution."""
        pid = self._resolve_premise(premise_id)
        await self.async_authenticate()
        url = (
            f"{SUPPLIER_API_BASE}//elements/my-energy/premises/{pid}/measurement-ranges"
        )
        payload = await self._async_get_json(url, label="measurement-ranges")
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict) and isinstance(payload.get("ranges"), list):
            return payload["ranges"]
        raise VattenfallApiError("Unexpected measurement-ranges payload shape")

    async def async_get_last_finalized_day(
        self, premise_id: str | None = None
    ) -> date | None:
        """Return the most recent date with finalized hourly measurements.

        Reads ``measurement-ranges`` and inspects the ``Hourly`` resolution
        entries' ``lastAvailableMeasurementDate`` (epoch seconds, UTC midnight
        of the day in question). When multiple entries exist (e.g. consumption
        + microproduction meters) the *minimum* timestamp is used so we never
        treat a not-yet-uploaded day as finalized.

        Returns ``None`` if the API response cannot be parsed.
        """
        ranges = await self._async_with_auth_retry(
            lambda: self.async_get_measurement_ranges(premise_id=premise_id)
        )
        return _last_finalized_day_from_ranges(ranges)

    # ---- HTTP helpers ------------------------------------------------------

    async def _async_get_json(self, url: str, *, label: str) -> Any:
        client = await self._async_get_client()
        headers = self._browser_headers(
            origin=WEB_BASE,
            referer=f"{WEB_BASE}/",
            sec_fetch_dest="empty",
            sec_fetch_mode="cors",
            sec_fetch_site="same-site",
        )
        headers["accept"] = "application/json"
        last_resp: httpx.Response | None = None
        for attempt in range(1, _MAX_SERVER_ERROR_RETRIES + 1):
            _LOGGER.debug("supplier api: GET %s (attempt %d)", url, attempt)
            resp = await client.get(url, headers=headers, follow_redirects=False)
            last_resp = resp
            if resp.status_code < 500 or attempt == _MAX_SERVER_ERROR_RETRIES:
                break
            await asyncio.sleep(_RETRY_DELAY_S)

        assert last_resp is not None
        if last_resp.status_code in (401, 403):
            raise VattenfallAuthError(
                f"Unauthorized response from {label} (HTTP {last_resp.status_code})"
            )
        if last_resp.status_code >= 400:
            raise VattenfallApiError(
                f"{label} failed with HTTP {last_resp.status_code}: {last_resp.text[:200]}"
            )
        try:
            return last_resp.json()
        except ValueError as err:
            raise VattenfallApiError(f"{label} returned non-JSON body: {err}") from err

    # ---- Cookie + header helpers ------------------------------------------

    def _cookie(self, name: str, *, domain_hint: str | None = None) -> str | None:
        if self._client is None:
            return None
        for cookie in self._client.cookies.jar:
            if cookie.name != name:
                continue
            if domain_hint and domain_hint not in (cookie.domain or ""):
                continue
            return cookie.value
        return None

    def _extract_session_data_key(self) -> str | None:
        """Find the most recently set ``sessionNonceCookie-<key>`` and return ``<key>``."""
        if self._client is None:
            return None
        candidates: list[str] = []
        for cookie in self._client.cookies.jar:
            if cookie.name.startswith(SESSION_NONCE_PREFIX):
                candidates.append(cookie.name.removeprefix(SESSION_NONCE_PREFIX))
        if not candidates:
            return None
        # When multiple are present, pick the last (most recently set).
        return candidates[-1]

    @staticmethod
    def _browser_headers(
        *,
        content_type_form: bool = False,
        origin: str = WEB_BASE,
        referer: str | None = None,
        sec_fetch_dest: str | None = None,
        sec_fetch_mode: str | None = None,
        sec_fetch_site: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "origin": origin,
            "referer": referer or f"{origin}/",
            "user-agent": DEFAULT_USER_AGENT,
        }
        if content_type_form:
            headers["content-type"] = "application/x-www-form-urlencoded"
        if sec_fetch_dest:
            headers["sec-fetch-dest"] = sec_fetch_dest
        if sec_fetch_mode:
            headers["sec-fetch-mode"] = sec_fetch_mode
        if sec_fetch_site:
            headers["sec-fetch-site"] = sec_fetch_site
        return headers


# --- Parsers (module-level for unit testability) -----------------------------


def _parse_premises(payload: Any) -> list[Premise]:
    """Extract premises from a ``my-premises-select`` payload.

    The payload shape isn't fully documented; we accept either a top-level list
    or a dict with a ``premises``/``items`` list. Each entry is expected to
    expose a ``premiseId`` (or similar) field. Unknown fields are preserved on
    ``Premise.raw`` so callers can drill in if needed.
    """
    raw_items: list[dict[str, Any]] = []
    if isinstance(payload, list):
        raw_items = [item for item in payload if isinstance(item, dict)]
    elif isinstance(payload, dict):
        for key in ("premises", "items", "result", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                raw_items = [item for item in value if isinstance(item, dict)]
                break
        else:
            # Sometimes a single premise is returned as a dict.
            if any(k in payload for k in ("premiseId", "PremiseId")):
                raw_items = [payload]

    out: list[Premise] = []
    for item in raw_items:
        pid = (
            item.get("premiseId")
            or item.get("PremiseId")
            or item.get("id")
        )
        if not pid:
            continue
        addr = (
            item.get("address")
            or item.get("Address")
            or item.get("displayAddress")
            or item.get("formattedAddress")
        )
        if isinstance(addr, dict):
            # Flatten common nested address shapes.
            addr = ", ".join(
                str(v)
                for v in (
                    addr.get("street"),
                    addr.get("streetAddress"),
                    addr.get("city"),
                    addr.get("postalCode") or addr.get("zip"),
                )
                if v
            ) or None
        out.append(Premise(premise_id=str(pid), address=addr if isinstance(addr, str) else None, raw=item))
    return out


def _parse_hourly(payload: Any) -> list[HourlyConsumptionPoint]:
    items = _measurement_items(payload)
    out: list[HourlyConsumptionPoint] = []
    for item in items:
        try:
            year = int(item["year"])
            month = int(item["month"])
            day = int(item["day"])
            hour = int(item.get("hour", 0))
            value = float(item["measurement"]["value"])
        except (KeyError, TypeError, ValueError):
            continue
        dt = datetime(year, month, day, hour)
        out.append(HourlyConsumptionPoint(date_time=dt.isoformat(), value_kwh=value))
    out.sort(key=lambda p: p.date_time)
    return out


def _parse_daily(payload: Any) -> list[DailyConsumptionPoint]:
    items = _measurement_items(payload)
    out: list[DailyConsumptionPoint] = []
    for item in items:
        try:
            year = int(item["year"])
            month = int(item["month"])
            day = int(item["day"])
            value = float(item["measurement"]["value"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(DailyConsumptionPoint(date=f"{year:04d}-{month:02d}-{day:02d}", value_kwh=value))
    out.sort(key=lambda p: p.date)
    return out


def _last_finalized_day_from_ranges(ranges: list[dict[str, Any]]) -> date | None:
    """Extract the most recent fully-finalized day from a measurement-ranges payload.

    Vattenfall returns a list of range dicts, one per resolution (and per meter
    when several are present). For the ``Hourly`` resolution the
    ``lastAvailableMeasurementDate`` field is a Unix epoch in seconds pointing
    at UTC midnight of the last day where every hour has been uploaded.

    We take the *minimum* across all ``Hourly`` entries (most conservative)
    and return that date.
    """
    last_epochs: list[float] = []
    for entry in ranges:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("resolution", "")).lower() != "hourly":
            continue
        last = entry.get("lastAvailableMeasurementDate")
        if isinstance(last, (int, float)):
            last_epochs.append(float(last))
    if not last_epochs:
        return None
    epoch = min(last_epochs)
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).date()
    except (OSError, OverflowError, ValueError):
        return None


def _measurement_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [it for it in items if isinstance(it, dict)]
