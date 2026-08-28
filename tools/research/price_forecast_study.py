"""Multi-region feasibility study for week-ahead price forecasting.

Reproduces every empirical claim in ``docs/price_forecast_week_ahead.md``.
Downloads ~20 months of day-ahead prices and ERA5 reanalysis weather for eight
structurally different European bidding zones, then measures whether a simple
weather-anomaly model beats a day-class climatology baseline out of sample.

All sources are free and keyless (Energy-Charts, Elering, Open-Meteo). A full
run takes a few minutes and caches to ``--cache-dir`` so re-runs are instant.

Usage:
    python tools/research/price_forecast_study.py --cache-dir /tmp/pfstudy
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import pickle
import time
import urllib.request

import numpy as np

# Bidding zone → (Energy-Charts bzn, country code, UTC offset, weather points).
# Points approximate each zone's wind-fleet / demand centroid.
ZONES: dict[str, tuple[str, str, int, list[tuple[float, float]]]] = {
    "DE-LU":    ("DE-LU",    "de", 1, [(53.9, 8.5), (51.0, 10.0)]),
    "BE":       ("BE",       "be", 1, [(51.3, 3.0), (50.6, 4.4)]),
    "ES":       ("ES",       "es", 1, [(41.5, -4.7), (37.4, -5.9)]),
    "LT":       ("LT",       "lt", 2, [(55.9, 21.3), (56.0, 23.3)]),
    "PL":       ("PL",       "pl", 1, [(54.4, 17.5), (52.0, 19.0)]),
    "IT-North": ("IT-North", "it", 1, [(45.5, 9.2), (44.8, 11.5)]),
    "FR":       ("FR",       "fr", 1, [(48.5, 2.5), (43.6, 4.5)]),
    "NO2":      ("NO2",      "no", 1, [(58.9, 6.5), (59.5, 8.0)]),
}

START, END = "2025-01-01", "2026-08-20"

# Export stops paying below the tariff wedge, not below zero — see the doc §1.3.
NEG_THRESHOLD_EUR_KWH = 0.025

# Trailing days used to fit the climatology baseline and the anomaly model.
TRAIN_DAYS_FULL = 270
TRAIN_DAYS_SHORT = 200


def _get(url: str, retries: int = 5) -> dict:
    """Fetch JSON with exponential backoff, tolerating the public rate limits.

    Args:
        url: Endpoint to fetch.
        retries: Attempts before giving up.

    Returns:
        Decoded JSON payload.

    Raises:
        RuntimeError: When every attempt fails.
    """
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=120) as fh:
                return json.load(fh)
        except Exception as err:  # noqa: BLE001 - transient HTTP, retried below
            if attempt == retries - 1:
                raise RuntimeError(f"failed: {url}") from err
            time.sleep(15 * (attempt + 1))
    raise RuntimeError("unreachable")


def _cached(path: str, build):
    """Return a pickled artefact, building and caching it on first use."""
    if os.path.exists(path):
        with open(path, "rb") as fh:
            return pickle.load(fh)
    value = build()
    with open(path, "wb") as fh:
        pickle.dump(value, fh)
    return value


def fetch_prices() -> dict[str, dict[int, float]]:
    """Download day-ahead prices per zone, keyed by unix second (EUR/MWh)."""
    out: dict[str, dict[int, float]] = {}
    for name, (bzn, _cc, _tz, _pts) in ZONES.items():
        points: dict[int, float] = {}
        for lo, hi in ((START, "2025-12-31"), ("2026-01-01", END)):
            data = _get(f"https://api.energy-charts.info/price?bzn={bzn}&start={lo}&end={hi}")
            for sec, price in zip(data["unix_seconds"], data["price"]):
                if price is not None:
                    points[sec] = price
            time.sleep(12)
        out[name] = points
        print(f"  prices {name:9s} n={len(points)}", flush=True)
    return out


def fetch_weather() -> dict[str, dict]:
    """Download ERA5 hourly wind / radiation / temperature per zone."""
    out: dict[str, dict] = {}
    for name, (_bzn, _cc, _tz, pts) in ZONES.items():
        acc: dict | None = None
        for lat, lon in pts:
            hourly = _get(
                f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}"
                f"&start_date={START}&end_date={END}"
                "&hourly=wind_speed_100m,shortwave_radiation,temperature_2m&timezone=UTC"
            )["hourly"]
            if acc is None:
                acc = {"time": hourly["time"], "w": [], "r": [], "t": []}
                for key in ("w", "r", "t"):
                    acc[key] = [[] for _ in hourly["time"]]
            for key, src in (("w", "wind_speed_100m"), ("r", "shortwave_radiation"),
                             ("t", "temperature_2m")):
                for i, val in enumerate(hourly[src]):
                    acc[key][i].append(val if val is not None else 0.0)
            time.sleep(8)
        out[name] = acc
        print(f"  weather {name:9s} hours={len(acc['time'])}", flush=True)
    return out


def power_curve(speed_kmh: np.ndarray) -> np.ndarray:
    """Convert 100 m wind speed to normalised turbine output.

    Output rises with the cube of speed between cut-in and rated, saturates at
    rated, and is zero outside the operating band. Using the curve rather than
    raw speed is what makes the wind feature discriminate — see doc §3.1.

    Args:
        speed_kmh: Wind speed at 100 m, km/h.

    Returns:
        Array of normalised output in [0, 1].
    """
    v = speed_kmh / 3.6
    ramp = np.clip((v - 3.0) / 9.0, 0.0, 1.0) ** 3
    return np.where((v < 3.0) | (v > 25.0), 0.0, np.where(v >= 12.0, 1.0, ramp))


def _band(sorted_prices: np.ndarray, fraction: float, top: bool) -> float:
    """Mean of the highest or lowest ``fraction`` of a day's sorted prices."""
    k = max(1, int(round(len(sorted_prices) * fraction)))
    return float(np.mean(sorted_prices[-k:] if top else sorted_prices[:k]))


def evaluate_zone(zone: str, prices: dict[int, float], weather: dict) -> dict[str, float]:
    """Measure out-of-sample skill of the anomaly model versus climatology.

    Walks forward one day at a time. The baseline is the trailing day-class
    median; the model adds a least-squares fit on wind / radiation /
    temperature anomalies plus a weekend interaction.

    Args:
        zone: Bidding-zone label.
        prices: Unix-second → EUR/MWh.
        weather: ERA5 payload for the zone.

    Returns:
        Target name → percentage MAE improvement over climatology (negative
        means the model is worse than doing nothing).
    """
    tz = dt.timezone(dt.timedelta(hours=ZONES[zone][2]))
    by_day: dict[dt.date, list[float]] = collections.defaultdict(list)
    for sec, price in prices.items():
        by_day[dt.datetime.fromtimestamp(sec, tz).date()].append(price / 1000.0)
    days = {d: np.sort(np.array(v)) for d, v in by_day.items() if len(v) >= 24}

    wind = power_curve(np.array(weather["w"], dtype=float)).mean(axis=1)
    rad = np.array(weather["r"], dtype=float).mean(axis=1)
    temp = np.array(weather["t"], dtype=float).mean(axis=1)
    dw, dr, dt_ = (collections.defaultdict(list) for _ in range(3))
    for i, stamp in enumerate(weather["time"]):
        day = dt.datetime.fromisoformat(stamp).replace(tzinfo=dt.UTC).astimezone(tz).date()
        dw[day].append(wind[i])
        dr[day].append(rad[i])
        dt_[day].append(temp[i])

    common = sorted(set(days) & set(dw))
    n = len(common)
    train = TRAIN_DAYS_FULL if n >= 400 else TRAIN_DAYS_SHORT
    wd = np.array([np.mean(dw[d]) for d in common])
    rd = np.array([np.mean(dr[d]) for d in common])
    td = np.array([np.mean(dt_[d]) for d in common])
    weekend = np.array([1.0 if d.weekday() >= 5 else 0.0 for d in common])

    targets = {
        "peak3": np.array([_band(days[d], 3 / 24, True) for d in common]),
        "trough3": np.array([_band(days[d], 3 / 24, False) for d in common]),
        "neg_h": np.array([float((days[d] < NEG_THRESHOLD_EUR_KWH).sum()) / len(days[d]) * 24
                           for d in common]),
    }

    result: dict[str, float] = {}
    for name, y in targets.items():
        err_clim, err_model = [], []
        for i in range(train, n):
            win = slice(i - train, i)
            base_w, base_r, base_t = wd[win].mean(), rd[win].mean(), td[win].mean()
            is_we = weekend[win]
            med_we = np.median(y[win][is_we == 1]) if (is_we == 1).any() else np.median(y[win])
            med_wd = np.median(y[win][is_we == 0]) if (is_we == 0).any() else np.median(y[win])
            clim = med_we if weekend[i] == 1 else med_wd
            design = np.column_stack([
                np.ones(train), wd[win] - base_w, (rd[win] - base_r) / 100.0,
                td[win] - base_t, is_we, (wd[win] - base_w) * is_we,
            ])
            beta, *_ = np.linalg.lstsq(design, y[win] - np.where(is_we == 1, med_we, med_wd),
                                       rcond=None)
            row = np.array([1.0, wd[i] - base_w, (rd[i] - base_r) / 100.0, td[i] - base_t,
                            weekend[i], (wd[i] - base_w) * weekend[i]])
            err_clim.append(abs(y[i] - clim))
            err_model.append(abs(y[i] - (clim + float(row @ beta))))
        result[name] = 100.0 * (1.0 - np.mean(err_model) / np.mean(err_clim))
    return result


def main() -> None:
    """Run the study and print the per-zone skill table."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache-dir", default="/tmp/pfstudy")
    args = ap.parse_args()
    os.makedirs(args.cache_dir, exist_ok=True)

    prices = _cached(os.path.join(args.cache_dir, "prices.pkl"), fetch_prices)
    weather = _cached(os.path.join(args.cache_dir, "weather.pkl"), fetch_weather)

    print(f"\n{'zone':9s} {'peak3':>8s} {'trough3':>8s} {'neg_h':>8s}")
    print("-" * 36)
    for zone in ZONES:
        skill = evaluate_zone(zone, prices[zone], weather[zone])
        print(f"{zone:9s} {skill['peak3']:+7.1f}% {skill['trough3']:+7.1f}% {skill['neg_h']:+7.1f}%")
    print("\nPositive = model beats climatology. See docs/price_forecast_week_ahead.md")


if __name__ == "__main__":
    main()
