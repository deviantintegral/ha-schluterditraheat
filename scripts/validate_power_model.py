#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "aiohttp>=3.8.0",
#     "async-timeout>=4.0.0",
# ]
# ///
"""Check the power model against the cloud's own hourly consumption.

The power sensor computes ``load_watt x percent``. Two attributes could supply
that percent: ``outputPercentDisplay`` (what the integration currently uses) and
``floorSetpointPwm`` (fetched on every poll but never parsed). If the wrong one
is being integrated, the resulting energy is wrong -- which is what a comparison
against the cloud's hourly watt-hours showed.

This script samples *both* candidates over one or more complete clock hours,
converts each into a predicted watt-hours for the hour, and scores them against
the actual watt-hours the consumption endpoint reports for that same hour. The
candidate that matches is the one the sensor should be using.

Dependencies are declared inline (PEP 723), so uv fetches them into a throwaway
environment -- nothing to install, and Home Assistant is never imported.

Usage:
    export SCHLUTER_USERNAME=you@example.com
    export SCHLUTER_PASSWORD=...
    uv run scripts/validate_power_model.py --probe          # one-shot, ~5 requests
    uv run scripts/validate_power_model.py                  # sample 1 hour, then score

The shebang also carries `uv run --script`, so ./scripts/validate_power_model.py
works directly. Plain `python3 scripts/validate_power_model.py` still works too,
provided aiohttp is already available.

Run it while the floor is actually heating. An idle hour reads zero on every
candidate and discriminates nothing.

Two cautions:

* The cloud enforces a daily request cap. Sampling every 60s costs ~60 requests
  per hour on top of whatever Home Assistant is already spending. Use
  --interval 120 to halve that; the duty-cycle average barely suffers.
* The cloud also caps concurrent sessions, and this script logs in as you. If it
  fails with a session-limit error, the Home Assistant integration is probably
  holding the session -- stop the integration (or just accept that one of the two
  will be logged out) and retry.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import types
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from getpass import getpass
from typing import Any

import aiohttp

# Load api.py without executing the integration's __init__.py, which imports
# Home Assistant -- this script needs to run with nothing but aiohttp installed.
# Registering a synthetic parent package lets api.py's `from .const import ...`
# resolve while the real __init__.py stays untouched.
_INTEGRATION = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "custom_components",
    "schluterditraheat",
)
_pkg = types.ModuleType("_schluter")
_pkg.__path__ = [_INTEGRATION]
sys.modules["_schluter"] = _pkg

_api = importlib.import_module("_schluter.api")
SchluterApi = _api.SchluterApi
SchluterApiError = _api.SchluterApiError

# The two attributes that could plausibly be the duty cycle to integrate.
CANDIDATES = ["outputPercentDisplay", "floorSetpointPwm"]


def coerce_percent(value: Any) -> float | None:
    """Pull a percentage out of an attribute whose shape we don't know.

    The API is inconsistent: some attributes are bare numbers, others wrap the
    value in ``{"value": n}`` or ``{"percent": n}``. Return None when there is
    no number to be had, so a missing attribute is distinguishable from a real
    zero -- that distinction is the whole point of this exercise.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in ("percent", "value"):
            inner = value.get(key)
            if isinstance(inner, (int, float)):
                return float(inner)
    return None


def load_watt(raw: dict[str, Any]) -> float:
    """Sum the connected load across both outputs, as the integration does."""
    total = 0.0
    for key in ("loadWattOutput1", "loadWattOutput2"):
        watts = coerce_percent(raw.get(key))
        if watts:
            total += watts
    return total


async def probe(api: SchluterApi, device_id: int) -> dict[str, Any]:
    """Dump one raw attribute payload plus recent consumption. ~5 requests."""
    raw = await api.get_device_attributes(device_id)
    print("\n=== Raw attribute payload ===")
    print(json.dumps(raw, indent=2, sort_keys=True))

    watts = load_watt(raw)
    print("\n=== Candidate percentages, right now ===")
    for name in CANDIDATES:
        print(f"  {name:<24} raw={raw.get(name)!r:<28} -> {coerce_percent(raw.get(name))}")
    print(f"  {'load_watt (sum)':<24} {watts} W")

    consumption = await api.get_consumption_history(device_id, "hourly")
    points = api.parse_consumption_history(consumption)
    print(f"\n=== Cloud hourly consumption (last {min(12, len(points))} of {len(points)}) ===")
    print(f"  {'hour (UTC)':<22}{'Wh':>8}{'implied duty %':>17}")
    for start, kwh in points[-12:]:
        wh = kwh * 1000
        duty = f"{100 * wh / watts:>16.1f}" if watts else "  (no load_watt)"
        print(f"  {start.strftime('%Y-%m-%d %H:%M'):<22}{wh:>8.1f}{duty}")

    if watts:
        print(
            "\n'implied duty' is what the percentage MUST have averaged for the cloud's\n"
            "watt-hours to be right. Whichever candidate above tracks that column is the\n"
            "one the power sensor should multiply by. Sample a full hour (drop --probe)\n"
            "to compare properly."
        )
    return raw


async def sample(
    api: SchluterApi, device_id: int, interval: int, hours: int
) -> dict[datetime, list[dict[str, float | None]]]:
    """Poll both candidates until `hours` complete clock hours have been covered.

    Samples are bucketed by UTC hour. Only hours we covered end to end are
    returned -- a partially sampled hour would understate whichever candidate
    happened to be low during the sampled part.
    """
    buckets: dict[datetime, list[dict[str, float | None]]] = defaultdict(list)
    now = datetime.now(timezone.utc)
    # Start of the next full hour: the first hour we can cover completely.
    first_full = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    end = first_full + timedelta(hours=hours)

    print(
        f"\nSampling every {interval}s until {end:%Y-%m-%d %H:%M} UTC "
        f"({hours} complete hour(s) starting {first_full:%H:%M}).\n"
        f"Roughly {int((end - now).total_seconds() // interval)} requests. Ctrl-C to stop early;\n"
        f"whatever complete hours exist will still be scored.\n"
    )

    try:
        while datetime.now(timezone.utc) < end:
            stamp = datetime.now(timezone.utc)
            try:
                raw = await api.get_device_attributes(device_id)
            except SchluterApiError as err:
                print(f"  {stamp:%H:%M:%S}  request failed: {err}")
                await asyncio.sleep(interval)
                continue

            row: dict[str, float | None] = {
                name: coerce_percent(raw.get(name)) for name in CANDIDATES
            }
            row["load_watt"] = load_watt(raw)
            buckets[stamp.replace(minute=0, second=0, microsecond=0)].append(row)

            shown = "  ".join(f"{n}={row[n]}" for n in CANDIDATES)
            print(f"  {stamp:%H:%M:%S}  {shown}  load={row['load_watt']}")
            await asyncio.sleep(interval)
    except KeyboardInterrupt:
        print("\nInterrupted -- scoring the complete hours collected so far.")

    # Keep only hours that are fully in the past and well covered.
    expected = 3600 / interval
    complete = {
        hour: rows
        for hour, rows in buckets.items()
        if hour >= first_full
        and hour + timedelta(hours=1) <= datetime.now(timezone.utc)
        and len(rows) >= expected * 0.9
    }
    return complete


def mean(values: list[float | None]) -> float | None:
    """Mean of the samples that actually carried a number."""
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present) / len(present)


async def score(
    api: SchluterApi,
    device_id: int,
    hourly: dict[datetime, list[dict[str, float | None]]],
) -> None:
    """Compare each candidate's predicted watt-hours against the cloud's actual."""
    if not hourly:
        print("\nNo complete hours were sampled -- nothing to score.")
        return

    # The freshest bucket can still be partial on the cloud's side; give it a moment.
    print("\nWaiting 5 minutes for the cloud to finalize the last hourly bucket...")
    await asyncio.sleep(300)

    consumption = await api.get_consumption_history(device_id, "hourly")
    actual = {start: kwh * 1000 for start, kwh in api.parse_consumption_history(consumption)}

    print("\n=== Predicted vs actual watt-hours ===")
    header = f"{'hour (UTC)':<18}{'n':>4}{'actual Wh':>11}{'implied %':>11}"
    for name in CANDIDATES:
        header += f"{name[:14] + ' %':>18}{'pred Wh':>10}{'err':>8}"
    print(header)

    totals: dict[str, list[float]] = {name: [] for name in CANDIDATES}

    for hour in sorted(hourly):
        rows = hourly[hour]
        if hour not in actual:
            print(f"{hour:%m-%d %H:%M}  no cloud bucket for this hour -- skipped")
            continue

        watts = mean([r["load_watt"] for r in rows]) or 0.0
        actual_wh = actual[hour]
        implied = 100 * actual_wh / watts if watts else float("nan")

        line = f"{hour:%m-%d %H:%M}  {len(rows):>4}{actual_wh:>11.1f}{implied:>11.1f}"
        for name in CANDIDATES:
            pct = mean([r[name] for r in rows])
            if pct is None or not watts:
                line += f"{'n/a':>18}{'-':>10}{'-':>8}"
                continue
            pred_wh = watts * pct / 100
            err = 100 * (pred_wh - actual_wh) / actual_wh if actual_wh else float("nan")
            totals[name].append(abs(err))
            line += f"{pct:>18.1f}{pred_wh:>10.1f}{err:>7.0f}%"
        print(line)

    print("\n=== Verdict ===")
    ranked = []
    for name in CANDIDATES:
        if totals[name]:
            avg = sum(totals[name]) / len(totals[name])
            ranked.append((avg, name))
            print(f"  {name:<24} mean absolute error {avg:>6.1f}%")
        else:
            print(f"  {name:<24} never carried a usable number")

    if not ranked:
        print("\n  Neither candidate produced a number. Was the floor idle the whole time?")
        return

    ranked.sort()
    best_err, best = ranked[0]
    print(
        f"\n  Closest match: {best}"
        + (f" ({best_err:.1f}% mean error)" if best_err else "")
    )
    if best_err < 10:
        print(f"  -> The power sensor should multiply load_watt by {best}.")
    else:
        print(
            "  -> Neither candidate is convincing. The percentage may not be an\n"
            "     integrable duty cycle at all, or the load_watt constant is wrong.\n"
            "     Compare the 'implied %' column against both candidates by hand."
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--device-id", type=int, help="default: the first thermostat found")
    parser.add_argument("--interval", type=int, default=60, help="seconds between samples (default 60)")
    parser.add_argument("--hours", type=int, default=1, help="complete hours to sample (default 1)")
    parser.add_argument("--probe", action="store_true", help="dump one payload and exit; no sampling")
    args = parser.parse_args()

    username = os.environ.get("SCHLUTER_USERNAME") or input("Schluter username: ")
    password = os.environ.get("SCHLUTER_PASSWORD") or getpass("Schluter password: ")

    async with aiohttp.ClientSession() as session:
        api = SchluterApi(session, username, password)
        try:
            await api.authenticate()
        except SchluterApiError as err:
            print(f"Login failed: {err}", file=sys.stderr)
            print(
                "A session-limit error means Home Assistant (or the app) is holding the\n"
                "session -- stop the integration and retry.",
                file=sys.stderr,
            )
            return 1

        devices = await api.get_static_data()
        if not devices:
            print("No thermostats found on this account.", file=sys.stderr)
            return 1

        device_id = args.device_id or next(iter(devices))
        info = devices[device_id]
        print(f"Device {device_id}: {info.get('group_name') or info.get('name')}")

        await probe(api, device_id)
        if args.probe:
            return 0

        hourly = await sample(api, device_id, args.interval, args.hours)
        await score(api, device_id, hourly)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
