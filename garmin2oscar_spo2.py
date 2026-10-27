#!/usr/bin/env python3
"""
garmin2oscar_spo2.py - Downloads SpO2 data from Garmin Connect (via
python-garminconnect) and creates the files to import into OSCAR, plus a readable .csv.
By default one Viatom/Wellue file per night is written to ./oscar_viatom/:
in OSCAR, "Import Viatom/Wellue data", select all files, done.
With --format md300: .dat files for the Oximetry Wizard, one night at a time.

Requirements:
    Python 3.12+   and   pip install garminconnect
    fit2oscar_spo2.py must be in the same folder as this script.

Usage:
    python garmin2oscar_spo2.py                     # last night
    python garmin2oscar_spo2.py --night 2026-10-18  # night of Oct 18 to 19
    python garmin2oscar_spo2.py --nights 7          # the last 7 nights

How it works:
  1. Logs in to Garmin Connect. The first time, you are asked for your
     e-mail, password and, if enabled, your two-factor code. Session tokens
     are then stored in ~/.garminconnect (the password itself is never saved).
  2. Downloads the "original" .fit files for the evening's day and the next
     day (a night spans two daily files). They are kept in
     ./garmin_fit/YYYY-MM-DD/ and are not downloaded again later.
  3. Downloads Garmin's per-minute sleep movement (not in the .fit files);
     with the default Viatom format it appears as OSCAR's Movement graph.
  4. Converts them for OSCAR (1 reading per minute, same as the manual export).
  5. Writes the sleep stages (deep, light, REM, awake) of every night into
     ./oscar_dreem/garmin_sleep_stages.csv, imported with "Import Dreem data".
  6. Writes Garmin's breathing disruptions into ./breathing/ (.csv files for a
     spreadsheet: OSCAR cannot display them).
  7. Writes garmin_nights_summary.csv: one row per night with Garmin's sleep,
     restlessness and interruptions scores, awakenings, restless moments,
     stage minutes, SpO2 summary and breathing disruptions.
  The files gathering several nights (summary, sleep stages, breathing,
  Somnopose) are cumulative: each run adds its nights and updates the ones
  it covers again, so running the script every day builds up a history
  (--overwrite starts them from scratch). OSCAR skips nights it already has.
  Also oscar_journal/garmin_journal.xml for OSCAR's File > Import Journal
  (--no-journal to skip it): a note per night (scores, HRV, resting HR, stages, SpO2, breathing,
  Body Battery...) and bookmarks on breathing disruptions and the lowest SpO2.
  The note and the summary also give the activity of the day before each
  night (steps, distance, active time, intensity minutes, activities...),
  from Garmin Connect's daily summary or the WELLNESS .fit totals.
  With --somnopose, also a file for OSCAR's "Import Somnopose data":
  breathing disruptions as an Orientation graph, restless moments as an
  Inclination graph.
  If the .fit download fails, the script falls back to the Garmin Connect
  SpO2 JSON API (sometimes at a lower resolution).

Warning: python-garminconnect uses Garmin Connect's private API. This is not
officially allowed by Garmin and may stop working at any time.
"""

import argparse
import datetime as dt
import getpass
import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import fit2oscar_spo2 as f2o
except ImportError:
    sys.exit("fit2oscar_spo2.py not found: put it in the same folder as this script.")
if not hasattr(f2o, "to_local"):
    sys.exit("fit2oscar_spo2.py is an outdated version: replace it with the latest one.")

UNIX_TO_FIT = 631065600  # seconds between 1970-01-01 and 1989-12-31


def login(tokenstore):
    try:
        from garminconnect import Garmin, GarminConnectAuthenticationError
    except ImportError:
        sys.exit("Missing module: install it with  pip install garminconnect  (Python 3.12+)")

    tokenstore = os.path.expanduser(tokenstore)
    if os.path.isdir(tokenstore) and os.listdir(tokenstore):
        try:
            api = Garmin()
            api.login(tokenstore)
            print("Logged in to Garmin Connect (saved session).")
            return api
        except Exception as e:
            print(f"Saved session unusable ({e}), a new login is required.")

    email = os.getenv("GARMIN_EMAIL") or input("Garmin Connect e-mail: ").strip()
    password = os.getenv("GARMIN_PASSWORD") or getpass.getpass("Password: ")
    os.makedirs(tokenstore, mode=0o700, exist_ok=True)
    api = Garmin(email, password,
                 prompt_mfa=lambda: input("Two-factor authentication code: ").strip())
    try:
        api.login(tokenstore)
    except GarminConnectAuthenticationError as e:
        sys.exit(f"Login failed: {e}")
    print(f"Logged in. Session saved in {tokenstore}")
    return api


def fetch_fit_day(api, day, fit_dir, offline=False, log=print):
    """Returns the day's .fit files, downloading them if needed."""
    folder = os.path.join(fit_dir, day.isoformat())
    cached = [os.path.join(folder, f) for f in os.listdir(folder)] if os.path.isdir(folder) else []
    cached = [p for p in cached if p.lower().endswith(".fit")]
    # A day already in the cache is not downloaded again, except today/yesterday
    # (the watch may have synced new data since).
    recent = day >= dt.date.today() - dt.timedelta(days=1)
    if cached and (offline or not recent):
        return cached
    if offline or api is None:
        return cached

    log(f"Downloading .fit files for {day:%Y-%m-%d}...")
    data = api.download_health_snapshot(day.isoformat())
    os.makedirs(folder, exist_ok=True)
    paths = []
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                if name.lower().endswith(".fit"):
                    p = os.path.join(folder, os.path.basename(name))
                    with open(p, "wb") as f:
                        f.write(z.read(name))
                    paths.append(p)
    elif data[8:12] == b".FIT":
        p = os.path.join(folder, f"{day.isoformat()}_WELLNESS.fit")
        with open(p, "wb") as f:
            f.write(data)
        paths.append(p)
    else:
        raise ValueError("unexpected response (neither ZIP nor FIT)")
    return paths


def to_fit_ts(value):
    """Converts a Garmin JSON timestamp (Unix ms or GMT string) to FIT seconds."""
    if isinstance(value, (int, float)):
        return int(value / 1000) - UNIX_TO_FIT
    if isinstance(value, str):
        t = dt.datetime.fromisoformat(value.replace("Z", "")).replace(tzinfo=dt.timezone.utc)
        return int(t.timestamp()) - UNIX_TO_FIT
    return None


def fetch_json_day(api, day, log=print):
    """Fallback: SpO2 and pulse via the JSON API. Returns (readings, hr)."""
    readings, hr = [], {}
    data = api.get_spo2_data(day.isoformat()) or {}
    for item in data.get("continuousReadingDTOList") or []:
        ts = to_fit_ts(item.get("epochTimestamp") or item.get("readingTimeGMT"))
        val = item.get("spo2Reading") or item.get("value")
        if ts is not None and val and 30 <= val <= 100:
            readings.append((ts, int(val), int(item.get("readingConfidence") or 0)))
    if not readings:  # otherwise, spot readings or hourly averages
        for key in ("spO2SingleValues", "spO2HourlyAverages"):
            for pair in data.get(key) or []:
                if isinstance(pair, (list, tuple)) and len(pair) >= 2 and pair[1]:
                    readings.append((to_fit_ts(pair[0]), int(pair[1]), 0))
            if readings:
                log(f"  (JSON API: only '{key}' available, reduced resolution)")
                break
    hrdata = api.get_heart_rates(day.isoformat()) or {}
    for pair in hrdata.get("heartRateValues") or []:
        if pair and len(pair) >= 2 and pair[1]:
            hr[to_fit_ts(pair[0])] = int(pair[1])
    return readings, hr


def fetch_sleep_json(api, wake_day, fit_dir, offline=False, log=print):
    """Garmin Connect sleep data for the night ending on wake_day (or None).

    Cached as <fit_dir>/<wake_day>/sleep.json, so it is downloaded only once.
    """
    path = os.path.join(fit_dir, wake_day.isoformat(), "sleep.json")
    recent = wake_day >= dt.date.today() - dt.timedelta(days=1)
    if os.path.exists(path) and (offline or not recent or api is None):
        with open(path) as f:
            return json.load(f)
    if api is None or offline:
        return None
    try:
        data = api.get_sleep_data(wake_day.isoformat()) or {}
    except Exception as e:
        log(f"Sleep data unavailable for {wake_day:%Y-%m-%d} ({e}).")
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)
    return data


def fetch_day_json(api, day, fit_dir, offline=False, log=print):
    """Garmin Connect daily summary + activities of a day (cached as day.json)."""
    path = os.path.join(fit_dir, day.isoformat(), "day.json")
    recent = day >= dt.date.today() - dt.timedelta(days=1)
    if os.path.exists(path) and (offline or not recent or api is None):
        with open(path) as f:
            return json.load(f)
    if api is None or offline:
        return None
    data = {}
    try:
        data["stats"] = api.get_stats(day.isoformat()) or {}
    except Exception as e:
        log(f"Daily summary unavailable for {day:%Y-%m-%d} ({e}).")
    try:
        data["activities"] = api.get_activities_by_date(day.isoformat(), day.isoformat()) or []
    except Exception as e:
        log(f"Activity list unavailable for {day:%Y-%m-%d} ({e}).")
    if not data:
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)
    return data


def movement_from_json(data):
    """Per-minute sleep movement: {fit_minute_start_utc: activity level}."""
    movement = {}
    for item in (data or {}).get("sleepMovement") or []:
        ts = to_fit_ts(item.get("startGMT"))
        level = item.get("activityLevel")
        if ts is not None and isinstance(level, (int, float)):
            movement[ts - ts % 60] = float(level)
    return movement


def local_to_fit(day, hour=12, minute=0, tz=None):
    """A local wall-clock time on the given day, as FIT UTC seconds."""
    t = dt.datetime(day.year, day.month, day.day, hour, minute)
    if isinstance(tz, int):
        unix = t.replace(tzinfo=dt.timezone.utc).timestamp() - tz
    elif tz is None:
        unix = t.astimezone().timestamp()
    else:
        unix = t.replace(tzinfo=tz).timestamp()
    return int(unix) - UNIX_TO_FIT


def day_length_hours(day, tz):
    """23, 24 or 25: length of a local calendar day (DST changes)."""
    return (local_to_fit(day + dt.timedelta(days=1), 0, 0, tz)
            - local_to_fit(day, 0, 0, tz)) / 3600


def process_night(api, night, a):
    days = [night, night + dt.timedelta(days=1)]
    print(f"\n=== Night of {days[0]:%Y-%m-%d} to {days[1]:%Y-%m-%d} ===")

    paths, use_json = [], False
    for d in days:
        try:
            paths += fetch_fit_day(api, d, a.fit_dir, a.offline)
        except Exception as e:
            print(f"Could not download .fit files for {d:%Y-%m-%d} ({e}).")
            use_json = True

    readings, hr, _ = f2o.extract_many(paths) if paths else ([], {}, None)

    tz = f2o.make_tz(a.timezone, a.tz_offset)
    window = (local_to_fit(night, 12, 0, tz), local_to_fit(days[1], 12, 0, tz))

    # Sleep data (movement and stages) from Garmin Connect, cached as sleep.json
    sleep_json = None
    if not (a.no_movement and a.no_stages and a.no_breathing and not a.somnopose and a.no_summary):
        sleep_json = fetch_sleep_json(api, days[1], a.fit_dir, a.offline)

    in_window = lambda sessions: [x for x in sessions if window[0] <= x["start"] < window[1]]
    json_stages = in_window(f2o.sleep_from_json(sleep_json, to_fit_ts))
    fit_stages = []
    for p in paths:
        try:
            fit_stages += f2o.extract_sleep(p)
        except Exception:
            pass
    fit_stages = in_window(fit_stages)
    restless = in_window(f2o.restless_from_json(sleep_json, to_fit_ts))
    extras, resp = f2o.extras_from_json(sleep_json, to_fit_ts), {}

    # Activity of the day before the night (the evening's date): Garmin
    # Connect's daily summary first, otherwise the WELLNESS .fit totals.
    day_before = {}
    if not a.no_day_before:
        daily = {}
        for p in paths:
            try:
                f2o.merge_daily(daily, f2o.extract_daily_activity(p, tz))
            except Exception:
                pass
        fit_days = f2o.daily_totals_from_fit(daily)
        if night.isoformat() in fit_days:
            day_before[night.isoformat()] = fit_days[night.isoformat()]
        day_json = fetch_day_json(api, night, a.fit_dir, a.offline)
        if day_json:
            jt = f2o.daily_totals_from_json(day_json.get("stats"), day_json.get("activities"))
            if jt:
                day_before[night.isoformat()] = jt
    if a.journal:
        for p in paths:
            try:
                extras += f2o.extract_extras(p)
                resp.update(f2o.extract_respiration(p))
            except Exception:
                pass
    stages = []
    if not a.no_stages:
        stages = json_stages or fit_stages   # JSON first, otherwise SLEEP_DATA .fit files
        midnight = local_to_fit(days[1], 0, 0, tz)
        if (day_length_hours(night, tz) != 24 and not a.keep_dst_evening
                and any(x["start"] < midnight for x in stages)):
            # same OSCAR time-axis issue as for the oximetry (see below)
            stages = [c for c in (f2o.clip_session(x, midnight) for x in stages) if c]
            print("Daylight saving time changed that day: sleep stages start at midnight.")
        if stages:
            print("Sleep stages found.")

    breathing = f2o.disruptions_from_json(sleep_json, to_fit_ts)
    if not breathing:                        # otherwise SLEEP_DISRUPTIONS .fit files
        for p in paths:
            try:
                breathing += f2o.extract_disruptions(p)
            except Exception:
                pass
    breathing = in_window(breathing)

    if (use_json or not readings) and api is not None and not a.offline:
        print("Falling back to the Garmin Connect SpO2 JSON API...")
        for d in days:
            try:
                r, h = fetch_json_day(api, d)
                readings += r
                hr.update(h)
            except Exception as e:
                print(f"  JSON API unavailable for {d:%Y-%m-%d} ({e})")
        readings = list({r[0]: r for r in readings if r[0] is not None}.values())

    result = {"created": [], "stages": stages, "breathing": breathing, "restless": restless,
              "all_stages": json_stages + fit_stages, "extras": extras, "resp": resp, "days": day_before,
              "readings": [r for r in readings if window[0] <= r[0] < window[1]]}
    if not readings:
        print("No SpO2 readings for this night.")
        return result

    # Same window as OSCAR's day split: noon to noon (local time)
    midnight = local_to_fit(days[1], 0, 0, tz)

    if a.from_time:
        h, m = (int(x) for x in a.from_time.split(":"))
        cut = local_to_fit(night if h >= 12 else days[1], h, m, tz)
        readings = [r for r in readings if r[0] >= cut]
        window = (min(window[0], cut), window[1])
        print(f"Readings before {a.from_time} ignored (--from-time).")
    elif (day_length_hours(night, tz) != 24 and not a.keep_dst_evening
          and any(window[0] <= r[0] < midnight for r in readings)):
        # OSCAR labels the time axis from midnight of the day of the first
        # session. If that day is a 23 h or 25 h day (DST change), every label
        # of the night is 1 hour off. Starting the oximetry at midnight avoids it.
        before = sum(1 for r in readings if window[0] <= r[0] < midnight)
        readings = [r for r in readings if not (window[0] <= r[0] < midnight)]
        print(f"Daylight saving time changed on {night:%Y-%m-%d}: the {before} "
              "readings taken before midnight were dropped, otherwise OSCAR "
              "would show this night's time axis 1 hour off.")

    movement = {} if a.no_movement else movement_from_json(sleep_json)
    if movement:
        print(f"Sleep movement: {len(movement)} minutes.")

    created = f2o.convert(readings, hr, tz, a.outdir, a.min_confidence, a.min_minutes,
                          a.max_hold, a.split_gap, not a.no_csv, window=window,
                          fmt=a.format, movement=movement)
    if not created:
        print("No overnight recording long enough (see --min-minutes).")
    result["created"] = created
    return result


def main():
    yesterday = dt.date.today() - dt.timedelta(days=1)
    ap = argparse.ArgumentParser(description="Garmin Connect (SpO2) -> .dat for OSCAR")
    ap.add_argument("--night", type=dt.date.fromisoformat, default=yesterday,
                    help="date of the evening the night starts, YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--nights", type=int, default=1,
                    help="number of nights to process, going back from --night")
    ap.add_argument("-o", "--outdir", default=".", help="output folder for .dat/.csv files")
    ap.add_argument("--fit-dir", default="garmin_fit", help="cache folder for downloaded .fit files")
    ap.add_argument("--tokenstore", default="~/.garminconnect")
    ap.add_argument("--offline", action="store_true",
                    help="only use .fit files already in the cache, no login")
    ap.add_argument("--min-confidence", type=int, default=0)
    ap.add_argument("--min-minutes", type=int, default=30)
    ap.add_argument("--max-hold", type=int, default=120)
    ap.add_argument("--split-gap", type=int, default=10)
    ap.add_argument("--timezone", default=None,
                    help="time zone, e.g. Europe/Paris (default: the computer's)")
    ap.add_argument("--tz-offset", type=float, default=None)
    ap.add_argument("--no-csv", action="store_true", help="do not write the .csv file")
    ap.add_argument("--from-time", default=None, metavar="HH:MM",
                    help="ignore readings before this local time (e.g. 00:00)")
    ap.add_argument("--format", choices=["viatom", "md300"], default="viatom",
                    help="viatom (default): all nights in one import; md300: Oximetry Wizard .dat")
    ap.add_argument("--no-movement", action="store_true",
                    help="don't download Garmin's sleep movement")
    ap.add_argument("--no-stages", action="store_true",
                    help="don't write the sleep-stage file (Dreem format)")
    ap.add_argument("--no-breathing", action="store_true",
                    help="don't write the breathing-disruption .csv files")
    ap.add_argument("--somnopose", "--breathing-as-position", dest="somnopose", action="store_true",
                    help="also write a Somnopose file: breathing disruptions as OSCAR's "
                         "Orientation graph (0 not measured, 10 none, 20 low, 30 medium, "
                         "40 high) and restless moments as its Inclination graph")
    ap.add_argument("--no-summary", action="store_true",
                    help="don't write garmin_nights_summary.csv")
    ap.add_argument("--no-journal", dest="journal", action="store_false",
                    help="don't write the OSCAR Journal file (notes and bookmarks per night)")
    ap.add_argument("--journal", dest="journal", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--no-day-before", action="store_true",
                    help="don't fetch the activity of the day before each night")
    ap.add_argument("--overwrite", action="store_true",
                    help="start the cumulative files (summary, stages, breathing, Somnopose) "
                         "from scratch instead of adding to them")
    ap.add_argument("--keep-dst-evening", action="store_true",
                    help="keep readings before midnight on DST-change nights")
    a = ap.parse_args()

    api = None if a.offline else login(a.tokenstore)
    res = {"created": [], "stages": [], "breathing": [], "restless": [], "all_stages": [], "readings": [],
           "extras": [], "resp": {}, "days": {}}
    for i in range(a.nights - 1, -1, -1):
        r = process_night(api, a.night - dt.timedelta(days=i), a)
        for k in res:
            if k in ("resp", "days"):
                res[k].update(r[k])
            else:
                res[k] += r[k]
    created, stages, breathing = res["created"], res["stages"], res["breathing"]

    if a.format == "md300":
        print(f"\n{len(created)} .dat file(s) ready for OSCAR"
              + (": Oximetry Wizard > Import from a data file." if created else "."))
    elif created:
        print(f"\n{len(created)} night(s) ready in {os.path.join(a.outdir, 'oscar_viatom')}\n"
              "In OSCAR: Import Viatom/Wellue data, open that folder, select all files "
              "(Cmd+A) and confirm. Nights already imported are skipped.")
    else:
        print("\nNo oximetry file created.")
    if stages:
        tz = f2o.make_tz(a.timezone, a.tz_offset)
        print()
        f2o.write_dreem_csv(os.path.join(a.outdir, "oscar_dreem", "garmin_sleep_stages.csv"),
                            stages, tz, overwrite=a.overwrite)
        print("In OSCAR: Import Dreem data and pick that file. Nights already imported are skipped.")
    if breathing and not a.no_breathing:
        print()
        f2o.write_breathing_csv(a.outdir, breathing, f2o.make_tz(a.timezone, a.tz_offset),
                                overwrite=a.overwrite)
        print("(Open these .csv files in a spreadsheet.)")
    tz = f2o.make_tz(a.timezone, a.tz_offset)
    if a.somnopose and (breathing or res["restless"]):
        print()
        path = f2o.write_somnopose_csv(
            os.path.join(a.outdir, "oscar_somnopose", "garmin_somnopose.csv"),
            breathing, res["restless"], tz, a.keep_dst_evening, overwrite=a.overwrite)
        if path:
            print("In OSCAR: Import Somnopose data and pick that file. Nights already imported "
                  "are skipped.\n  Orientation graph = breathing disruptions "
                  "(0 not measured, 10 none, 20 low, 30 medium, 40 high)\n"
                  "  Inclination graph = restless moments (one spike each)")
    if a.journal:
        print()
        if f2o.write_journal_xml(os.path.join(a.outdir, "oscar_journal", "garmin_journal.xml"),
                                 res["readings"], res["all_stages"], breathing, res["restless"],
                                 res["extras"], res["resp"], tz, overwrite=a.overwrite,
                                 days=res["days"]):
            print("In OSCAR: File > Backup Journal first. Then display a day that is NOT in this file\n"
                  "  (e.g. before your first Garmin night) whose Notes box is NOT empty (type a\n"
                  "  temporary 'x' if needed, remove it afterwards), then File > Import Journal.\n"
                  "  OSCAR's import relies on the note box on screen: if it is empty, the notes\n"
                  "  of the imported days are erased; if the day on screen is in the file, its\n"
                  "  note is replaced.")
    if not a.no_summary:
        print()
        f2o.write_summary_csv(os.path.join(a.outdir, "garmin_nights_summary.csv"), res["readings"],
                              res["all_stages"], breathing, res["restless"], tz,
                              overwrite=a.overwrite, days=res["days"])
        print("(One row per night: Garmin scores, awakenings, restless moments, stages, SpO2 "
              "and breathing. Open it in a spreadsheet.)")


if __name__ == "__main__":
    main()
