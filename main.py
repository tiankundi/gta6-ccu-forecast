"""Forecast GTA VI's launch-day Twitch viewers, hour by hour, from hour 1's print.

Method (calibrated on the case's own comparables at run time, stdlib only):

  * The release-day curve of a rolling-midnight console launch has a stable
    shape when expressed as log ratios to hour 1: a dip in hours 2-5 (most of
    the world still asleep), a climb as the US unlocks (East 05:00Z, West
    08:00Z), a peak around hours 19-21 (US afternoon / EU evening), then a
    gentle fall.  That shape is measured from the case's `comparables`:
    past big launches' hourly Twitch curves, restricted to the console-only
    reference class (platforms with no PC and no Stadia -- the same launch
    shape as GTA VI: PS5 / Xbox Series, rolling local-midnight unlock,
    single-player).  Multi-platform launches with a simultaneous PC unlock
    spike hour 1 and decay, a different shape, so they are left out.
  * Centre of each hour: the trimmed mean (drop the outer 20% each side) of
    log(viewers_h / viewers_1) over the console-only comparables, then a
    scale adjustment: centre_h += slope_h * (log p1 - mean log p1 of the
    group), where slope_h is the per-hour OLS slope of the log ratios on
    log hour-1 viewers, fit on the group itself.  The day's growth depends
    on launch size (peak log ratio correlates about -0.85 with log hour-1
    viewers across the console launches), so a big midnight print flattens
    the shape and a small one steepens it.  In a leave-one-out backtest on
    the 8 console-only launches in the practice kits this cut the pinball
    loss from 0.323 (house persistence baseline) to 0.170, from 0.205 for
    the unadjusted shape; the two worst launches improved from 0.367/0.318
    to 0.159/0.170.
  * Spread: q_tau(h) = p1 * exp(centre_h + z_tau * s_h) with
    s_h = 0.15 + 0.07 * sqrt(h - 1) (z the standard normal quantiles
    -1.645, -0.674, 0, 0.674, 1.645).  The schedule was picked on the same
    backtest: it tracks the observed cross-launch spread of the log ratios
    (about 0.12-0.32 early, about 0.42-0.47 late) without over-fitting a
    single hour's noisy sample spread.
  * Fallbacks, in order: console-only class -> all comparables -> flat
    persistence (the house baseline's shape).  A case never crashes the run.

The program reads every <id>.in in the input directory and writes one
<id>.out per case: {"hours": [{"hour": h, "q": [q05, q25, q50, q75, q95]}]}
for hours 2..24.  Deterministic: no randomness, no wall clock, sorted
iteration only.  Runs in well under the 8 s cap on one CPU.

Run locally: mkdir -p out && MAKEFUN_INPUT_DIR=input MAKEFUN_OUTPUT_DIR=out python3 main.py
"""

import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

INPUT_DIR = Path(os.environ.get("MAKEFUN_INPUT_DIR", "/input"))
OUTPUT_DIR = Path(os.environ.get("MAKEFUN_OUTPUT_DIR", "/output"))

LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.645, -0.674, 0.0, 0.674, 1.645]
# Log-spread schedule: s_h = SPREAD_A + SPREAD_B * sqrt(h - 1).
SPREAD_A = 0.15
SPREAD_B = 0.07
# A comparable joins the shape fit only with a fairly complete release day.
MIN_HOURS = 20
# At least this many launches in the console-only class, else fall back.
MIN_CLASS = 3


def _parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def round2(x):
    """Round to 2 decimals, half up, as JavaScript Math.round(x*100)/100 does."""
    scaled = x * 100
    whole = math.floor(scaled)
    if scaled - whole >= 0.5:
        whole += 1
    value = whole / 100
    return int(value) if float(value).is_integer() else value


def trimmed_mean(values):
    """Mean after dropping the outer 20% on each side (at least one each)."""
    vals = sorted(values)
    n = len(vals)
    if n <= 2:
        return sum(vals) / n
    k = max(1, n // 5)
    core = vals[k: n - k] if n - 2 * k >= 1 else vals
    return sum(core) / len(core)


def release_day_curve(comp):
    """Hour -> viewers for the 24 UTC hours of the comparable's release day."""
    try:
        day_start = _parse_time(comp["day_start"])
    except (KeyError, TypeError, ValueError):
        return {}
    by_hour = {}
    for point in comp.get("hourly") or []:
        try:
            t = _parse_time(point["from"])
            viewers = point["viewers"]
        except (KeyError, TypeError, ValueError):
            continue
        if viewers is None:
            continue
        delta_h = (t - day_start).total_seconds() / 3600.0
        if 0 <= delta_h < 24:
            by_hour[int(delta_h) + 1] = float(viewers)
    if len(by_hour) < MIN_HOURS:
        return {}
    if by_hour.get(1, 0) <= 0:
        return {}
    return by_hour


def is_console_only(comp):
    """True when the launch is console-only: no PC, no Stadia in platforms."""
    platforms = comp.get("platforms") or ""
    return "PC" not in platforms and "Stadia" not in platforms


def shape_centres(curves):
    """Trimmed-mean log ratio to hour 1, per hour, over a list of curves."""
    centres = {}
    for h in range(2, 25):
        ratios = [
            math.log(curve[h] / curve[1])
            for curve in curves
            if h in curve and curve[h] > 0
        ]
        if len(ratios) >= 2:
            centres[h] = trimmed_mean(ratios)
    return centres


def shape_slopes(curves):
    """Per-hour OLS slope of log ratio to hour 1 on log hour-1 viewers.

    Across the console-only launches the day's growth depends on launch
    size: the log ratio at the peak correlates about -0.85 with log hour-1
    viewers (a huge midnight print means the day is already near its peak;
    a small one leaves room to grow through the day).  The slope is fit on
    the same group as the centres, per hour, with no tuned constant; with
    fewer than three ratios at an hour the slope is 0 (no adjustment).
    """
    slopes = {}
    for h in range(2, 25):
        pairs = [
            (math.log(curve[1]), math.log(curve[h] / curve[1]))
            for curve in curves
            if h in curve and curve[h] > 0
        ]
        if len(pairs) < 3:
            slopes[h] = 0.0
            continue
        mean_x = sum(p[0] for p in pairs) / len(pairs)
        mean_y = sum(p[1] for p in pairs) / len(pairs)
        denom = sum((x - mean_x) ** 2 for x, _ in pairs)
        if denom == 0:
            slopes[h] = 0.0
        else:
            slopes[h] = (
                sum((x - mean_x) * (y - mean_y) for x, y in pairs) / denom
            )
    return slopes


def forecast_shape(case):
    """Return (centres, slopes, mean log hour-1) or None when no usable shape."""
    own_name = (case.get("game") or {}).get("name", "")
    comparables = case.get("comparables") or []
    console_curves = []
    all_curves = []
    for comp in comparables:
        # The GTA VI category's own pre-release history is a premiere-day
        # curve, not a launch day; the case's own game is never a comparable.
        if "Grand Theft Auto VI" in (comp.get("name") or ""):
            continue
        if own_name and comp.get("name") == own_name:
            continue
        curve = release_day_curve(comp)
        if not curve:
            continue
        all_curves.append(curve)
        if is_console_only(comp):
            console_curves.append(curve)
    for group in (console_curves, all_curves):
        if len(group) >= MIN_CLASS:
            centres = shape_centres(group)
            if len(centres) >= 20:
                mean_log_p1 = sum(
                    math.log(curve[1]) for curve in group
                ) / len(group)
                return centres, shape_slopes(group), mean_log_p1
    return None


def hour_quantiles(p1, centre, h):
    """Five quantiles for hour h around p1 * exp(centre)."""
    spread = SPREAD_A + SPREAD_B * math.sqrt(h - 1)
    quantiles = []
    prev = 0.0
    for z in Z:
        q = round2(p1 * math.exp(centre + z * spread))
        q = max(q, 1)
        q = min(q, 1_000_000_000)
        # Rounding must never break the non-decreasing order.
        if q < prev:
            q = prev
        quantiles.append(q)
        prev = q
    return quantiles


def baseline_quantiles(p1, h):
    """House persistence shape: every hour stays at hour 1's print."""
    return hour_quantiles(p1, 0.0, h)


def forecast_case(case, name):
    if case.get("levels") != LEVELS:
        raise ValueError(f"{name} asks for levels {case.get('levels')}, not {LEVELS}")
    first = next((k for k in case.get("known") or [] if k.get("hour") == 1), None)
    if first is not None:
        p1 = max(1.0, float(first["viewers"]))
    else:
        history = case.get("history") or []
        p1 = max(1.0, float(history[-1]["viewers"])) if history else 1.0
    shape = forecast_shape(case)
    hours = []
    for row in case["forecast"]:
        h = row["hour"]
        if shape is not None and h in shape[0]:
            centres, slopes, mean_log_p1 = shape
            # Scale adjustment: a case whose hour-1 print is above the
            # group's typical size grows less through the day (and vice
            # versa), per the per-hour slope fit on the group itself.
            centre = centres[h] + slopes[h] * (math.log(p1) - mean_log_p1)
            q = hour_quantiles(p1, centre, h)
        else:
            q = baseline_quantiles(p1, h)
        hours.append({"hour": h, "q": q})
    return {"hours": hours}


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(INPUT_DIR.glob("*.in")):
        case = json.loads(path.read_text(encoding="utf-8"))
        try:
            forecast = forecast_case(case, path.name)
        except Exception:
            # Never crash a case: fall back to the flat persistence forecast.
            first = next(
                (k for k in case.get("known") or [] if k.get("hour") == 1), None
            )
            p1 = max(1.0, float(first["viewers"])) if first is not None else 1.0
            forecast = {
                "hours": [
                    {"hour": row["hour"], "q": baseline_quantiles(p1, row["hour"])}
                    for row in case["forecast"]
                ]
            }
        (OUTPUT_DIR / f"{path.stem}.out").write_text(
            json.dumps(forecast, separators=(",", ":")) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
