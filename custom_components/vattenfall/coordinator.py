"""Data update coordinator for the Vattenfall supplier integration."""

from __future__ import annotations

import logging
import zoneinfo
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    DailyConsumptionPoint,
    HourlyConsumptionPoint,
    VattenfallApiClient,
    VattenfallApiError,
)
from .const import (
    ATTR_END_DATE,
    ATTR_HOURLY_END_DATE,
    ATTR_HOURLY_POINTS,
    ATTR_HOURLY_START_DATE,
    ATTR_POINTS,
    ATTR_START_DATE,
    CONF_PREMISE_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


_API_TIMEZONE = zoneinfo.ZoneInfo("Europe/Stockholm")
_CHUNK_MONTHS = 3
# Fallback used when measurement-ranges is unreachable: assume that any day
# older than two days ago is finalized.
_FALLBACK_FINALIZED_LAG_DAYS = 2


def _date_range_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split ``[start, end]`` into chunks of up to ``_CHUNK_MONTHS`` months each."""
    chunks: list[tuple[date, date]] = []
    chunk_start = start
    while chunk_start <= end:
        m = chunk_start.month - 1 + _CHUNK_MONTHS
        next_start = date(chunk_start.year + m // 12, m % 12 + 1, 1)
        chunk_end = min(next_start - timedelta(days=1), end)
        chunks.append((chunk_start, chunk_end))
        chunk_start = next_start
    return chunks


class VattenfallDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate periodic fetches from the Vattenfall supplier API."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: VattenfallApiClient,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=f"vattenfall_{entry.entry_id}",
            update_interval=DEFAULT_SCAN_INTERVAL,
        )
        self.client = client
        self.entry = entry

    @property
    def premise_id(self) -> str:
        return str(self.entry.data[CONF_PREMISE_ID])

    async def _async_resolve_finalized_day(self) -> date:
        """Fetch the last finalized day from the API, with a safe fallback."""
        try:
            finalized = await self.client.async_get_last_finalized_day()
        except VattenfallApiError as err:
            _LOGGER.debug(
                "measurement-ranges lookup failed (%s); using fallback lag", err
            )
            finalized = None
        if finalized is None:
            finalized = date.today() - timedelta(days=_FALLBACK_FINALIZED_LAG_DAYS)
        return finalized

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            finalized_day = await self._async_resolve_finalized_day()
            month_start = finalized_day.replace(day=1)
            today = date.today()
            # Cover finalized day's full set of hourly readings, plus partial
            # readings for any day that has come in since (typically today).
            hourly_start = min(finalized_day, today - timedelta(days=1))
            hourly_end = today

            data = await self._async_build_data(
                finalized_day=finalized_day,
                daily_start=month_start,
                daily_end=finalized_day,
                hourly_start=hourly_start,
                hourly_end=hourly_end,
            )
        except VattenfallApiError as err:
            raise UpdateFailed(f"Failed to fetch data from Vattenfall API: {err}") from err

        # Opportunistically write external statistics for newly-finalized data
        # so the Energy dashboard stays current without requiring backfill.
        await self._async_write_incremental_statistics(data)
        return data

    async def async_backfill_range(
        self,
        start_date: date,
        end_date: date,
        mode: str = "all",
    ) -> None:
        """Backfill historical data into the HA recorder for a date range."""
        if end_date < start_date:
            raise ValueError("end_date must be on or after start_date")
        if end_date >= date.today():
            raise ValueError("end_date must be before today; cannot backfill future data")

        chunks = _date_range_chunks(start_date, end_date)

        daily_points: list[DailyConsumptionPoint] = []
        hourly_points: list[HourlyConsumptionPoint] = []

        if mode in ("daily", "all"):
            for chunk_start, chunk_end in chunks:
                chunk = await self.client.async_get_daily_consumption(chunk_start, chunk_end)
                daily_points.extend(chunk)
                _LOGGER.debug(
                    "Fetched daily chunk %s..%s (%d points)",
                    chunk_start, chunk_end, len(chunk),
                )

        if mode in ("hourly", "all"):
            for chunk_start, chunk_end in chunks:
                chunk = await self.client.async_get_hourly_consumption(chunk_start, chunk_end)
                hourly_points.extend(chunk)
                _LOGGER.debug(
                    "Fetched hourly chunk %s..%s (%d points)",
                    chunk_start, chunk_end, len(chunk),
                )

        if daily_points or hourly_points:
            await self._async_write_statistics(daily_points, hourly_points)

        await self.async_request_refresh()

    async def _async_build_data(
        self,
        *,
        finalized_day: date,
        daily_start: date,
        daily_end: date,
        hourly_start: date,
        hourly_end: date,
    ) -> dict[str, Any]:
        finalized_iso = finalized_day.isoformat()

        # --- daily (finalized) ----
        daily_points = await self.client.async_get_daily_consumption(daily_start, daily_end)
        daily_values = [p.value_kwh for p in daily_points]
        finalized_day_kwh = next(
            (p.value_kwh for p in reversed(daily_points) if p.date == finalized_iso),
            daily_values[-1] if daily_values else 0.0,
        )
        finalized_month_to_date_kwh = round(sum(daily_values), 3)
        finalized_average_daily_kwh = (
            round(finalized_month_to_date_kwh / len(daily_values), 3)
            if daily_values
            else 0.0
        )

        # --- hourly (rolling window covering finalized day + partial today) ----
        hourly_points = await self.client.async_get_hourly_consumption(
            hourly_start, hourly_end
        )

        finalized_hourly = [
            p for p in hourly_points if p.date_time.startswith(finalized_iso)
        ]
        finalized_hourly_values = [p.value_kwh for p in finalized_hourly]
        if finalized_hourly:
            peak_point = max(finalized_hourly, key=lambda p: p.value_kwh)
            finalized_day_peak_hour_kwh = round(peak_point.value_kwh, 3)
            finalized_day_peak_hour_time = peak_point.date_time
        else:
            finalized_day_peak_hour_kwh = 0.0
            finalized_day_peak_hour_time = None

        today_iso = date.today().isoformat()
        today_hourly = [p for p in hourly_points if p.date_time.startswith(today_iso)]
        today_partial_kwh = round(sum(p.value_kwh for p in today_hourly), 3)

        # Latest hour with a confirmed reading: the API pads partial days with
        # 0.0 placeholders, so to avoid reporting "0 kWh now" all day long we
        # walk back from the end of the window to the most recent hour whose
        # value is > 0, and only fall back to the very last point when *all*
        # points are zero (genuine no-load case).
        latest_hour_kwh = 0.0
        for p in reversed(hourly_points):
            if p.value_kwh > 0:
                latest_hour_kwh = p.value_kwh
                break

        return {
            "finalized_date": finalized_iso,
            "finalized_day_kwh": finalized_day_kwh,
            "finalized_month_to_date_kwh": finalized_month_to_date_kwh,
            "finalized_average_daily_kwh": finalized_average_daily_kwh,
            "finalized_day_peak_hour_kwh": finalized_day_peak_hour_kwh,
            "finalized_day_peak_hour_time": finalized_day_peak_hour_time,
            "today_partial_kwh": today_partial_kwh,
            "latest_hour_kwh": latest_hour_kwh,
            ATTR_START_DATE: daily_start.isoformat(),
            ATTR_END_DATE: daily_end.isoformat(),
            ATTR_POINTS: [asdict(point) for point in daily_points],
            ATTR_HOURLY_START_DATE: hourly_start.isoformat(),
            ATTR_HOURLY_END_DATE: hourly_end.isoformat(),
            ATTR_HOURLY_POINTS: [asdict(point) for point in hourly_points],
        }

    async def _async_write_incremental_statistics(self, data: dict[str, Any]) -> None:
        """Write external statistics for the latest finalized day only.

        Idempotent: ``async_add_external_statistics`` overwrites rows that share
        the same ``(statistic_id, start)`` key, so it's safe to run every poll.
        """
        finalized_iso: str | None = data.get("finalized_date")
        if not finalized_iso:
            return

        daily_points = [
            DailyConsumptionPoint(**raw)
            for raw in data.get(ATTR_POINTS, [])
            if isinstance(raw, dict) and raw.get("date") == finalized_iso
        ]
        hourly_points = [
            HourlyConsumptionPoint(**raw)
            for raw in data.get(ATTR_HOURLY_POINTS, [])
            if isinstance(raw, dict)
            and isinstance(raw.get("date_time"), str)
            and raw["date_time"].startswith(finalized_iso)
        ]

        if not daily_points and not hourly_points:
            return
        try:
            await self._async_write_statistics(daily_points, hourly_points)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Skipping incremental statistics write: %s", err)

    async def _async_write_statistics(
        self,
        daily_points: list[DailyConsumptionPoint],
        hourly_points: list[HourlyConsumptionPoint],
    ) -> None:
        """Write fetched data points as external statistics into the HA recorder."""
        from homeassistant.components.recorder.models import (  # noqa: PLC0415
            StatisticData,
            StatisticMeanType,
            StatisticMetaData,
        )
        from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
            async_add_external_statistics,
        )
        from homeassistant.const import UnitOfEnergy  # noqa: PLC0415

        statistic_prefix = self.premise_id.lower()

        if daily_points:
            statistic_id = f"{DOMAIN}:daily_consumption_{statistic_prefix}"
            first_day = datetime.fromisoformat(daily_points[0].date).date()
            range_start_dt = datetime(
                first_day.year, first_day.month, first_day.day, tzinfo=_API_TIMEZONE
            )
            last_sum = await self._async_last_sum_before(statistic_id, range_start_dt, "day")

            stats: list[StatisticData] = []
            cumsum = last_sum
            for point in daily_points:
                d = datetime.fromisoformat(point.date).date()
                start_dt = datetime(d.year, d.month, d.day, tzinfo=_API_TIMEZONE)
                cumsum = round(cumsum + point.value_kwh, 3)
                stats.append(StatisticData(start=start_dt, state=point.value_kwh, sum=cumsum))

            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name=f"Vattenfall Daily Consumption {self.premise_id}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            async_add_external_statistics(self.hass, metadata, stats)
            _LOGGER.debug("Wrote %d daily statistics for %s", len(stats), statistic_id)

        if hourly_points:
            statistic_id = f"{DOMAIN}:hourly_consumption_{statistic_prefix}"
            first_hour_dt = datetime.fromisoformat(hourly_points[0].date_time).replace(
                tzinfo=_API_TIMEZONE
            )
            last_sum = await self._async_last_sum_before(statistic_id, first_hour_dt, "hour")

            stats = []
            cumsum = last_sum
            for point in hourly_points:
                start_dt = datetime.fromisoformat(point.date_time).replace(tzinfo=_API_TIMEZONE)
                cumsum = round(cumsum + point.value_kwh, 3)
                stats.append(StatisticData(start=start_dt, state=point.value_kwh, sum=cumsum))

            metadata = StatisticMetaData(
                has_mean=False,
                has_sum=True,
                mean_type=StatisticMeanType.NONE,
                name=f"Vattenfall Hourly Consumption {self.premise_id}",
                source=DOMAIN,
                statistic_id=statistic_id,
                unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            )
            async_add_external_statistics(self.hass, metadata, stats)
            _LOGGER.debug("Wrote %d hourly statistics for %s", len(stats), statistic_id)

    async def _async_last_sum_before(
        self, statistic_id: str, before_dt: datetime, period: str
    ) -> float:
        """Return the cumulative sum of the last stat before ``before_dt``, or ``0``."""
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415
        from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
            statistics_during_period,
        )

        delta = timedelta(days=1) if period == "day" else timedelta(hours=1)
        existing = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            before_dt - delta,
            before_dt,
            {statistic_id},
            period,
            None,
            {"sum"},
        )
        rows = (existing or {}).get(statistic_id, [])
        return rows[-1].get("sum") or 0.0 if rows else 0.0
