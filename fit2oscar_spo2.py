#!/usr/bin/env python3
"""
fit2oscar_spo2.py - Converts SpO2 (and pulse) from Garmin .fit files
(WELLNESS / monitoring) into files OSCAR can import.

Default output: one Wellue/Viatom O2Ring file per night, in ./oscar_viatom/.
In OSCAR: Tools > Import Viatom/Wellue data, select every file in that folder
(Cmd+A): all nights are imported at once, nights already imported are skipped.
With --format md300: one ChoiceMMed MD300 .dat per night, to import one at a
time via Oximetry Wizard > Import from a data file.

Sleep stages (SLEEP_DATA .fit files) are written to
./oscar_dreem/garmin_sleep_stages.csv, one row per night, to import with
"Import Dreem data". OSCAR then shows a sleep-stage graph.

Garmin's breathing disruptions (SLEEP_DISRUPTIONS .fit files) are written to
./breathing/: one summary row per night and one row per period. OSCAR has no
place for them, so they are for a spreadsheet, next to the CPAP's AHI.

No external dependencies: Python 3.9+ is enough.

Usage:
    python fit2oscar_spo2.py FILE.fit [MORE.fit ...] [options]

Options:
    -o DIR               output directory (default: current directory)
    --min-confidence N   skip readings whose Garmin confidence is < N (default 0)
    --min-minutes N      skip segments shorter than N minutes (default 30)
    --max-hold N         max time (s) a reading is repeated for (default 120)
    --split-gap N        gap (min) beyond which a new file is started (default 10)
    --timezone NAME      time zone, e.g. Europe/Paris (default: the computer's)
    --tz-offset H        fixed offset in hours, no DST (rarely useful)
    --no-csv             do not write the human-readable .csv
    --format FMT         viatom (default) or md300
    --no-stages          do not write the sleep-stage file
    --no-breathing       do not write the breathing-disruption files
    --somnopose          also write a Somnopose file for OSCAR: breathing
                         disruptions as an "Orientation" graph (0 not measured,
                         10 none, 20 low, 30 medium, 40 high) and restless
                         moments as an "Inclination" graph (old name of this
                         option: --breathing-as-position)
    --no-summary         do not write garmin_nights_summary.csv (one row per
                         night: Garmin scores, counts, stages, SpO2, breathing)
    --no-journal         do not write oscar_journal/garmin_journal.xml (written
                         by default) for OSCAR's File > Import Journal: a note
                         per night (Garmin scores, HRV, stages, SpO2,
                         breathing, day before...)
    --overwrite          start the cumulative files from scratch (see below)
    --airsense [DEVICE]  read the ResMed AirSense 11 clock over Bluetooth (needs
                         airbreak-plus and airsense_clock_check.py) and shift
                         every Garmin time by its offset; DEVICE is saved in
                         ~/.garmin2oscar.json after the first use
    --airbreak PATH      airbreak-plus folder (saved too)
    --clock-offset S     shift every Garmin time by S seconds, without Bluetooth

The files gathering several nights (garmin_nights_summary.csv, the sleep
stages, the breathing files and the Somnopose file) are cumulative: each run
adds its nights and updates the ones it covers again, so running the script
every day builds up a history. OSCAR skips the nights it already has.

md300 .dat format (read by MD300W1Loader::readDATFile in OSCAR):
    byte 0: id, bytes 1-2: number of records (little-endian)
    then n records of 11 bytes:
        0 0 id YY MM DD hh mm ss SpO2 Pulse   (local time, 1 record per second)
    A value of 0 means no data (OSCAR breaks the trace there).
"""

import argparse
import datetime as dt
import json
import os
import struct
import sys

FIT_EPOCH = dt.datetime(1989, 12, 31, tzinfo=dt.timezone.utc)


# Clock correction (seconds) added to every time written for OSCAR, so that the
# Garmin data lines up with a CPAP whose clock is ahead (> 0) or behind (< 0).
CLOCK_OFFSET = 0


def set_clock_offset(seconds):
    """Shifts every time written by this module (see airsense_clock_check.py)."""
    global CLOCK_OFFSET
    CLOCK_OFFSET = int(round(seconds or 0))


def to_local(t, tz=None):
    """FIT timestamp (UTC) -> local time, daylight saving time included.

    tz: None = the computer's time zone, int = fixed offset in seconds,
        or a tzinfo object (e.g. zoneinfo.ZoneInfo("Europe/Paris")).
    The CPAP clock correction (CLOCK_OFFSET) is applied.
    """
    utc = FIT_EPOCH + dt.timedelta(seconds=t + CLOCK_OFFSET)
    if isinstance(tz, int):
        return (utc + dt.timedelta(seconds=tz)).replace(tzinfo=None)
    return utc.astimezone(tz).replace(tzinfo=None)

# Useful FIT messages / fields
MSG_MONITORING_INFO = 103   # 253 timestamp, 0 local_timestamp
MSG_MONITORING = 55         # 253 timestamp, 26 timestamp_16, 27 heart_rate
MSG_SPO2 = 269              # 253 timestamp, 0 reading_spo2, 1 reading_confidence, 2 mode

BASE_FMT = {  # base type -> (struct code, size, invalid value)
    0x00: ("B", 1, 0xFF), 0x01: ("b", 1, 0x7F), 0x02: ("B", 1, 0xFF),
    0x83: ("h", 2, 0x7FFF), 0x84: ("H", 2, 0xFFFF), 0x85: ("i", 4, 0x7FFFFFFF),
    0x86: ("I", 4, 0xFFFFFFFF), 0x0A: ("B", 1, 0x00), 0x8B: ("H", 2, 0x0000),
    0x8C: ("I", 4, 0x00000000), 0x0D: ("B", 1, 0xFF),
    0x88: ("f", 4, None), 0x89: ("d", 8, None),      # float32 / float64 (invalid = NaN)
}


def parse_fit(path):
    """Minimal FIT parser. Returns a list of (global_msg_num, {field_num: value})."""
    with open(path, "rb") as f:
        data = f.read()
    if len(data) < 12 or data[8:12] != b".FIT":
        raise ValueError(f"{path} is not a valid FIT file")
    hsize = data[0]
    data_size = struct.unpack_from("<I", data, 4)[0]
    pos, end = hsize, hsize + data_size
    defs, out = {}, []
    last_ts = None
    while pos < end:
        rh = data[pos]; pos += 1
        if rh & 0x80:  # compressed timestamp header
            local = (rh >> 5) & 0x03
            offset = rh & 0x1F
            if last_ts is not None:
                last_ts = last_ts + ((offset - (last_ts & 0x1F)) & 0x1F)
            d = defs[local]
            vals, pos = read_fields(data, pos, d)
            if last_ts is not None and 253 not in vals:
                vals[253] = last_ts
            out.append((d["gnum"], vals))
            continue
        local = rh & 0x0F
        if rh & 0x40:  # definition message
            dev = bool(rh & 0x20)
            arch = data[pos + 1]
            endian = ">" if arch == 1 else "<"
            gnum = struct.unpack_from(endian + "H", data, pos + 2)[0]
            nf = data[pos + 4]; pos += 5
            fields = []
            for _ in range(nf):
                fields.append((data[pos], data[pos + 1], data[pos + 2]))
                pos += 3
            devsize = 0
            if dev:
                nd = data[pos]; pos += 1
                for _ in range(nd):
                    devsize += data[pos + 1]
                    pos += 3
            defs[local] = {"gnum": gnum, "endian": endian, "fields": fields, "devsize": devsize}
        else:  # data message
            d = defs[local]
            vals, pos = read_fields(data, pos, d)
            if 253 in vals:
                last_ts = vals[253]
            out.append((d["gnum"], vals))
    return out


def read_fields(data, pos, d):
    vals = {}
    for num, size, btype in d["fields"]:
        fmt = BASE_FMT.get(btype)
        if fmt and size == fmt[1]:
            v = struct.unpack_from(d["endian"] + fmt[0], data, pos)[0]
            if (v == v) if fmt[2] is None else (v != fmt[2]):   # NaN != NaN
                vals[num] = v
        pos += size
    pos += d["devsize"]
    return vals, pos


def extract(path):
    """Returns (SpO2 list [(ts_utc, spo2, conf)], pulse dict {ts_utc: bpm}, local_offset_s)."""
    msgs = parse_fit(path)
    spo2, hr = [], {}
    tz_offset = None
    last_ts = None
    for gnum, v in msgs:
        if gnum == MSG_MONITORING_INFO and 253 in v and 0 in v and tz_offset is None:
            tz_offset = v[0] - v[253]
        if gnum == MSG_MONITORING:
            if 253 in v and 26 not in v:
                last_ts = v[253]
            if 26 in v and last_ts is not None:
                last_ts = last_ts + ((v[26] - last_ts) & 0xFFFF)
                if 27 in v and v[27] > 0:
                    hr[last_ts] = v[27]
            elif 27 in v and 253 in v and v[27] > 0:
                hr[v[253]] = v[27]
        if gnum == MSG_SPO2 and 253 in v and 0 in v:
            if 30 <= v[0] <= 100:
                spo2.append((v[253], v[0], v.get(1, 0)))
    return spo2, hr, (tz_offset or 0)


def nearest_hr(hr_times, hr, t, tol=90):
    import bisect
    i = bisect.bisect_left(hr_times, t)
    best = None
    for j in (i - 1, i):
        if 0 <= j < len(hr_times) and abs(hr_times[j] - t) <= tol:
            if best is None or abs(hr_times[j] - t) < abs(best - t):
                best = hr_times[j]
    return hr[best] if best is not None else 0


def build_segments(readings, split_gap):
    segs, cur = [], []
    for r in readings:
        if cur and r[0] - cur[-1][0] > split_gap:
            segs.append(cur); cur = []
        cur.append(r)
    if cur:
        segs.append(cur)
    return segs


def write_dat(path, seg, hr_times, hr, tz, max_hold):
    start, stop = seg[0][0], seg[-1][0] + 60
    nsec = min(stop - start, 65535)
    spo2_s = [0] * nsec
    for i, (t, val, _) in enumerate(seg):
        nxt = seg[i + 1][0] if i + 1 < len(seg) else t + 60
        for s in range(t, min(nxt, t + max_hold)):
            k = s - start
            if 0 <= k < nsec:
                spo2_s[k] = val
    out = bytearray(struct.pack("<BH", 0, nsec))
    rows = []
    for k in range(nsec):
        t = start + k
        o2 = spo2_s[k]
        pr = nearest_hr(hr_times, hr, t) if o2 else 0
        lt = to_local(t, tz)
        out += bytes([0, 0, 0, lt.year % 100, lt.month, lt.day,
                      lt.hour, lt.minute, lt.second, o2, min(pr, 255)])
        rows.append((lt, o2, pr))
    with open(path, "wb") as f:
        f.write(out)
    return rows


def per_second_spo2(seg, max_hold):
    """Spreads per-minute readings over seconds (0 = no data). Returns (start, list)."""
    start, stop = seg[0][0], seg[-1][0] + 60
    spo2_s = [0] * (stop - start)
    for i, (t, val, _) in enumerate(seg):
        nxt = seg[i + 1][0] if i + 1 < len(seg) else t + 60
        for sec in range(t, min(nxt, t + max_hold)):
            spo2_s[sec - start] = val
    return start, spo2_s


def motion_byte(movement, t):
    """Garmin sleep movement level for the minute containing t, x 10 (0-255)."""
    if not movement:
        return 0
    level = movement.get(t - t % 60)
    return 0 if level is None else max(0, min(255, round(level * 10)))


VIATOM_STEP = 4          # seconds per record, as in Wellue/Viatom O2Ring files
VIATOM_HEADER = 40


def write_viatom(path, seg, hr_times, hr, tz, max_hold, movement=None):
    """Writes a Wellue/Viatom O2Ring file (format 5), read by OSCAR's ViatomLoader.

    Header (40 bytes): u16 version=5, u16 year, month, day, hour, min, sec,
    u24 file size, 0, u16 duration (s), 0, 0, spo2 avg, spo2 min, ...,
    u16 time < 90 % (s), ..., avg pulse, zeros.
    Records (5 bytes, one per 4 s): spo2, pulse, invalid flag, motion, vibration.
    An invalid record is FF FF FF 00 00. OSCAR takes the start time from the
    14-digit local timestamp in the file name.
    movement: optional {fit_minute_start_utc: Garmin activity level}; stored in
    the motion byte as level x 10, shown by OSCAR as its Movement graph.
    """
    start, spo2_s = per_second_spo2(seg, max_hold)
    n = min(len(spo2_s) // VIATOM_STEP, 65535 // VIATOM_STEP)
    recs = bytearray()
    good_o2, good_hr = [], []
    for k in range(n):
        t = start + k * VIATOM_STEP
        o2 = spo2_s[k * VIATOM_STEP]
        pr = nearest_hr(hr_times, hr, t, tol=300) if o2 else 0
        mv = motion_byte(movement, t)
        if o2 and 30 <= pr <= 250:
            if o2 < 61:          # OSCAR's Viatom import ignores values below 61 %
                o2 = 0xFF
            else:
                good_o2.append(o2)
            good_hr.append(pr)
            recs += bytes([o2, pr, 0, mv, 0])
        else:                    # no SpO2, or no pulse nearby: gap in the trace
            recs += bytes([0xFF, 0xFF, 0xFF, mv, 0])
    lt = to_local(start, tz)
    size = VIATOM_HEADER + len(recs)
    hdr = bytearray(VIATOM_HEADER)
    struct.pack_into("<HHBBBBB", hdr, 0, 5, lt.year, lt.month, lt.day,
                     lt.hour, lt.minute, lt.second)
    hdr[9:12] = size.to_bytes(3, "little")
    struct.pack_into("<H", hdr, 13, n * VIATOM_STEP)
    if good_o2:
        hdr[17] = round(sum(good_o2) / len(good_o2))
        hdr[18] = min(good_o2)
        struct.pack_into("<H", hdr, 22, min(65535, VIATOM_STEP * sum(x < 90 for x in good_o2)))
    if good_hr:
        hdr[30] = min(255, round(sum(good_hr) / len(good_hr)))
    with open(path, "wb") as f:
        f.write(hdr + recs)


def convert(readings, hr, tz, outdir=".", min_confidence=0, min_minutes=30,
            max_hold=120, split_gap=10, csv=True, window=None, log=print,
            fmt="viatom", movement=None):
    """Converts readings into OSCAR oximetry files (plus a .csv if csv=True).

    readings: list of (fit_timestamp_utc, spo2, confidence)
    hr:       dict {fit_timestamp_utc: bpm}
    tz:       time zone (see to_local); None = the computer's
    window:   (start, end) as FIT UTC timestamps; only segments starting
              inside this window are written.
    movement: optional {fit_minute_start_utc: level} (Garmin sleep movement),
              written to Viatom files and to the .csv
    fmt:      "viatom" -> one file per night in <outdir>/oscar_viatom/, imported
                          all at once via "Import Viatom/Wellue data"
              "md300"  -> one .dat per night, imported via the Oximetry Wizard
    Returns the list of oximetry files created.
    """
    readings = sorted(r for r in readings if r[2] >= min_confidence)
    hr_times = sorted(hr)
    os.makedirs(outdir, exist_ok=True)
    vdir = os.path.join(outdir, "oscar_viatom")
    created = []
    kept = [seg for seg in build_segments(readings, split_gap * 60)
            if (seg[-1][0] - seg[0][0]) / 60 >= min_minutes
            and (not window or window[0] <= seg[0][0] < window[1])]
    # one file per night: the segments of a night go into the same file (the
    # gaps become "no data"), unless the night would exceed the 18 h a file holds
    merged = []
    for seg in kept:
        if (merged and night_of(seg[0][0], tz) == night_of(merged[-1][0][0], tz)
                and seg[-1][0] - merged[-1][0][0] < 18 * 3600):
            merged[-1] = merged[-1] + seg
        else:
            merged.append(seg)
    for seg in merged:
        lt0 = to_local(seg[0][0], tz)
        base = os.path.join(outdir, "garmin_spo2_" + lt0.strftime("%Y%m%d%H%M%S"))
        if fmt == "md300":
            out = base + ".dat"
            write_dat(out, seg, hr_times, hr, tz, max_hold)
        else:
            os.makedirs(vdir, exist_ok=True)
            out = os.path.join(vdir, "Garmin_" + lt0.strftime("%Y%m%d%H%M%S"))
            write_viatom(out, seg, hr_times, hr, tz, max_hold, movement)
        vals = [r[1] for r in seg]
        lt1 = to_local(seg[-1][0], tz)
        log(f"-> {out}: {lt0:%Y-%m-%d %H:%M} to {lt1:%Y-%m-%d %H:%M} (local time), "
            f"{len(seg)} readings, SpO2 avg {sum(vals)/len(vals):.1f}% min {min(vals)}%")
        if csv:
            with open(base + ".csv", "w") as f:
                f.write("Time,SpO2,Pulse,Confidence" + (",Movement" if movement else "") + "\n")
                for t, v, c in seg:
                    lt = to_local(t, tz)
                    line = f"{lt:%Y-%m-%d %H:%M:%S},{v},{nearest_hr(hr_times, hr, t)},{c}"
                    if movement:
                        mv = movement.get(t - t % 60)
                        line += "," + ("" if mv is None else f"{mv:.2f}")
                    f.write(line + "\n")
        created.append(out)
    return created


def extract_many(paths, log=print):
    """Reads several .fit files and merges their readings (duplicates are ignored)."""
    spo2, hr, tz = {}, {}, None
    for p in paths:
        try:
            s, h, off = extract(p)
        except Exception as e:  # corrupted or non-FIT file
            log(f"{os.path.basename(p)}: skipped ({e})")
            continue
        for r in s:
            spo2[r[0]] = r
        hr.update(h)
        if tz is None and s:
            tz = off
        if s:
            log(f"{os.path.basename(p)}: {len(s)} SpO2 readings, {len(h)} pulse readings")
    return list(spo2.values()), hr, tz


# ---------------------------------------------------------------------------
# Sleep stages -> Dreem CSV (OSCAR: "Import Dreem data")
# ---------------------------------------------------------------------------
MSG_EVENT = 21              # event 74 = sleep window start (type 0) / stop (type 1)
MSG_SLEEP_LEVEL = 275       # 253 timestamp = END of the interval, 0 sleep_level
MSG_SLEEP_ASSESSMENT = 346  # 11 awakenings_count
FIT_STAGES = {0: "NA", 1: "WAKE", 2: "Light", 3: "Deep", 4: "REM"}
FIT_SCORES = {"overall": 6, "quality": 7, "restlessness": 10, "interruptions": 14}  # msg 346
HRV_STATUS = {0: "none", 1: "poor", 2: "low", 3: "unbalanced", 4: "balanced"}
JSON_STAGES = {0: "Deep", 1: "Light", 2: "REM", 3: "WAKE"}   # Garmin Connect sleepLevels


def extract_sleep(path):
    """Sleep-stage sessions from a SLEEP_DATA .fit file.

    Returns a list of {"start", "stop", "intervals": [(t0, t1, stage)],
    "awakenings"} with FIT UTC timestamps. Each sleep_level message closes
    the interval that started at the previous one (the first one starts at
    the sleep window start, the last one is stamped at the window end).
    """
    starts, stops, levels, awakenings, scores = [], [], [], None, {}
    for gnum, v in parse_fit(path):
        if gnum == MSG_EVENT and v.get(0) == 74 and 253 in v:
            (starts if v.get(1, 0) == 0 else stops).append(v[253])
        elif gnum == MSG_SLEEP_LEVEL and 253 in v and 0 in v:
            levels.append((v[253], v[0]))
        elif gnum == MSG_SLEEP_ASSESSMENT:
            awakenings = v.get(11, awakenings)
            for key, num in FIT_SCORES.items():
                if num in v:
                    scores[key] = v[num]
            if 15 in v:
                scores["stress"] = v[15] / 100
    if not levels:
        return []
    levels.sort()
    start = starts[0] if starts else levels[0][0]
    stop = stops[-1] if stops else levels[-1][0]
    intervals, prev = [], start
    for t, lvl in levels:
        if t > prev:
            intervals.append((prev, min(t, stop), FIT_STAGES.get(lvl, "NA")))
        prev = max(prev, t)
    if not intervals:
        return []
    return [{"start": start, "stop": stop, "intervals": intervals, "awakenings": awakenings,
             "scores": scores}]


def sleep_from_json(data, to_fit_ts):
    """Sleep-stage session from Garmin Connect sleep JSON (sleepLevels)."""
    items = (data or {}).get("sleepLevels") or []
    intervals = []
    for it in items:
        t0, t1 = to_fit_ts(it.get("startGMT")), to_fit_ts(it.get("endGMT"))
        stage = JSON_STAGES.get(int(it.get("activityLevel", -1)) if isinstance(it.get("activityLevel"), (int, float)) else -1)
        if t0 is not None and t1 is not None and t1 > t0 and stage:
            intervals.append((t0, t1, stage))
    if not intervals:
        return []
    intervals.sort()
    dto = (data or {}).get("dailySleepDTO") or {}
    sc = dto.get("sleepScores") or {}
    scores = {}
    if isinstance((sc.get("overall") or {}).get("value"), (int, float)):
        scores["overall"] = sc["overall"]["value"]
    for key in ("restlessness", "awakeCount"):        # JSON only gives a rating word
        q = (sc.get(key) or {}).get("qualifierKey")
        if q:
            scores["restlessness" if key == "restlessness" else "interruptions"] = q.title()
    q = (sc.get("restlessness") or {}).get("qualifierKey")
    if q:
        scores["restlessness_rating"] = q.title()          # Garmin's own judgment
    ranges = {}                                        # Garmin's personal optimal ranges
    for key, name, div in (("deepPercentage", "deep", 1), ("remPercentage", "rem", 1),
                           ("lightPercentage", "light", 1), ("totalDuration", "total", 60),
                           ("awakeCount", "awakenings", 1), ("stress", "stress", 1)):
        r = sc.get(key) or {}
        lo, hi = r.get("optimalStart"), r.get("optimalEnd")
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
            ranges[name] = (lo / div, hi / div)
    return [{"start": intervals[0][0], "stop": intervals[-1][1], "intervals": intervals,
             "awakenings": dto.get("awakeCount"), "scores": scores, "ranges": ranges}]


def restless_from_json(data, to_fit_ts):
    """Restless moments (Garmin Connect sleep JSON): [{"start", "stop", "moments"}]."""
    data = data or {}
    moments = sorted(t for t in (to_fit_ts(it.get("startGMT"))
                                 for it in data.get("sleepRestlessMoments") or [])
                     if t is not None)
    if not moments:
        return []
    dto = data.get("dailySleepDTO") or {}
    start = to_fit_ts(dto.get("sleepStartTimestampGMT")) or moments[0]
    stop = to_fit_ts(dto.get("sleepEndTimestampGMT")) or moments[-1]
    count = data.get("restlessMomentsCount")
    return [{"start": min(start, moments[0]), "stop": max(stop, moments[-1]), "moments": moments,
             "count": count if isinstance(count, int) else len(moments)}]


def clip_session(sess, t_from):
    """Drops the part of a sleep-stage session before t_from (None if empty)."""
    iv = [(max(t0, t_from), t1, st) for t0, t1, st in sess["intervals"] if t1 > t_from]
    if not iv:
        return None
    return dict(sess, start=max(sess["start"], t_from), intervals=iv)

def _hms(sec):
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _iso(t, tz):
    utc = FIT_EPOCH + dt.timedelta(seconds=t + CLOCK_OFFSET)
    if isinstance(tz, int):
        local = utc.astimezone(dt.timezone(dt.timedelta(seconds=tz)))
    else:
        local = utc.astimezone(tz)
    return local.replace(microsecond=0).isoformat()


def stage_at(intervals, t):
    for t0, t1, st in intervals:
        if t0 <= t < t1:
            return st
    return "NA"


def dreem_row(sess, tz):
    """One Dreem CSV row (semicolon-separated) for a sleep-stage session."""
    start, stop, iv = sess["start"], sess["stop"], sess["intervals"]
    dur = {"Light": 0, "Deep": 0, "REM": 0, "WAKE": 0}
    onset_t = None
    for t0, t1, st in iv:
        if st in ("Light", "Deep", "REM") and onset_t is None:
            onset_t = t0
    onset_t = onset_t if onset_t is not None else start
    for t0, t1, st in iv:
        if st == "WAKE":
            dur["WAKE"] += max(0, t1 - max(t0, onset_t))
        elif st in dur:
            dur[st] += t1 - t0
    asleep = dur["Light"] + dur["Deep"] + dur["REM"]
    awakenings = sess.get("awakenings")
    if awakenings is None:
        awakenings = sum(1 for (t0, _, st) in iv if st == "WAKE" and t0 >= onset_t)
    efficiency = round(100 * asleep / (stop - start)) if stop > start else 0
    # OSCAR reads one stage per 30 s: first at the start time, then on every
    # 30-second boundary up to the stop time.
    samples, t = [stage_at(iv, start)], -(-start // 30) * 30
    if t == start:
        t += 30
    while t < stop:
        samples.append(stage_at(iv, t))
        t += 30
    return ";".join([
        "night", _iso(start, tz), _iso(stop, tz), _hms(onset_t - start), _hms(asleep),
        _hms(dur["Light"]), _hms(dur["Deep"]), _hms(dur["REM"]), _hms(dur["WAKE"]),
        str(int(awakenings)), str(efficiency), "[" + ",".join(samples) + "]",
    ])


DREEM_HEADER = ("Type;Start Time;Stop Time;Sleep Onset Duration;Sleep Duration;"
                "Light Sleep Duration;Deep Sleep Duration;REM Duration;"
                "Wake After Sleep Onset Duration;Number of awakenings;Sleep efficiency;Hypnogram")


def write_dreem_csv(path, sessions, tz, log=print, overwrite=False):
    """Writes every sleep-stage session (deduplicated) into one Dreem CSV."""
    uniq = []
    for sess in sorted(sessions, key=lambda x: x["start"]):
        if uniq and abs(sess["start"] - uniq[-1]["start"]) < 1800:
            continue                      # same night from two sources
        uniq.append(sess)
    if not uniq:
        return None
    report = save_rows(path, DREEM_HEADER, [dreem_row(sess, tz) for sess in uniq],
                       key=lambda r: _night_from_text(r.split(";")[1]), overwrite=overwrite, log=log)
    log(f"-> {path}: sleep stages, {report}")
    return path


def fit_to_ts_from_string(value):
    """Garmin JSON timestamp (Unix ms or GMT ISO string) -> FIT UTC seconds."""
    if isinstance(value, (int, float)):
        return int(value / 1000) - 631065600
    if isinstance(value, str):
        t = dt.datetime.fromisoformat(value.replace("Z", "")).replace(tzinfo=dt.timezone.utc)
        return int(t.timestamp()) - 631065600
    return None


# ---------------------------------------------------------------------------
# Breathing disruptions (Garmin "breathing variations") -> CSV
# ---------------------------------------------------------------------------
MSG_DISRUPTION_PERIOD = 470     # 253 timestamp = END of the period, 0 severity
MSG_DISRUPTION_OVERNIGHT = 471  # 0 severity for the whole night
SEVERITY = {0: "none", 1: "low", 2: "medium", 3: "high"}
SEVERITY_JSON = {"NONE": "none", "LOW": "low", "MINIMAL": "low", "MEDIUM": "medium",
                 "MODERATE": "medium", "HIGH": "high"}


def extract_disruptions(path):
    """Breathing-disruption periods from a SLEEP_DISRUPTIONS .fit file.

    Returns [{"start", "stop", "periods": [(t0, t1, severity)], "overall"}].
    Each period message closes the period begun at the previous one; a period
    without severity is "not measured" (as in Garmin Connect's own data).
    """
    marks, overall = [], None
    for gnum, v in parse_fit(path):
        if gnum == MSG_DISRUPTION_PERIOD and 253 in v:
            marks.append((v[253], SEVERITY.get(v[0], "not measured") if 0 in v else "not measured"))
        elif gnum == MSG_DISRUPTION_OVERNIGHT and 0 in v:
            overall = SEVERITY.get(v[0])
    marks.sort()
    if len(marks) < 2:
        return []
    periods = [(marks[i - 1][0], marks[i][0], marks[i][1])
               for i in range(1, len(marks)) if marks[i][0] > marks[i - 1][0]]
    return [{"start": marks[0][0], "stop": marks[-1][0], "periods": periods, "overall": overall}]


def disruptions_from_json(data, to_fit_ts):
    """Breathing-disruption periods from Garmin Connect sleep JSON."""
    periods = []
    for it in (data or {}).get("breathingDisruptionData") or []:
        t0, t1 = to_fit_ts(it.get("startGMT")), to_fit_ts(it.get("endGMT"))
        if t0 is None or t1 is None or t1 <= t0:
            continue
        periods.append((t0, t1, SEVERITY.get(it.get("value"), "not measured")))
    if not periods:
        return []
    periods.sort()
    dto = (data or {}).get("dailySleepDTO") or {}
    overall = SEVERITY_JSON.get(str(dto.get("breathingDisruptionSeverity") or "").upper())
    return [{"start": periods[0][0], "stop": periods[-1][1], "periods": periods, "overall": overall}]


SOMNOPOSE_VALUE = {"not measured": 0, "none": 10, "low": 20, "medium": 30, "high": 40}
SOMNOPOSE_EPOCH = 978307200 - 631065600   # 2001-01-01 UTC in FIT seconds


def _somno_t(t):
    """FIT seconds -> Somnopose timestamp (seconds since 2001), clock correction included."""
    return t - SOMNOPOSE_EPOCH + CLOCK_OFFSET


def _local_midnight(day, tz):
    """Local midnight starting the given date, as FIT UTC seconds."""
    t = dt.datetime(day.year, day.month, day.day)
    if isinstance(tz, int):
        unix = t.replace(tzinfo=dt.timezone.utc).timestamp() - tz
    elif tz is None:
        unix = t.astimezone().timestamp()
    else:
        unix = t.replace(tzinfo=tz).timestamp()
    return int(unix) - 631065600 - CLOCK_OFFSET


def night_of(t, tz):
    """The evening date (OSCAR's day, split at noon) a FIT timestamp belongs to."""
    lt = to_local(t, tz)
    return (lt - dt.timedelta(days=1)).date() if lt.hour < 12 else lt.date()


def _dedupe_by_night(sessions, tz):
    out = {}
    for sess in sorted(sessions, key=lambda x: x["start"]):
        out.setdefault(night_of(sess["start"], tz), sess)
    return out


def write_somnopose_csv(path, breathing, restless, tz, keep_dst_evening=False, log=print,
                        overwrite=False, night_recs=None):
    """Breathing disruptions and restless moments as a Somnopose file
    (OSCAR: "Import Somnopose data"), one session per night.

    OSCAR shows each column as its own graph, without touching the oximetry:
      Orientation = breathing disruptions: 0 not measured, 10 none, 20 low,
                    30 medium, 40 high
      Inclination = restless moments: a spike to 1 at each moment; on nights
                    recorded by a night app, the movement-based awakenings
                    instead: a spike to 1 per micro-awakening, a plateau at 2
                    during each awakening
    Timestamps are seconds since 2001-01-01 UTC; a "-" line ends a night. As
    for the oximetry, a night starting on a DST-change evening begins at
    midnight, otherwise OSCAR would label its time axis 1 hour off.
    """
    br, rs = _dedupe_by_night(breathing, tz), _dedupe_by_night(restless, tz)
    ev = _dedupe_by_night(night_recs or [], tz)
    blocks = []
    for night in sorted(set(br) | set(rs) | set(ev)):
        parts = [x for x in (br.get(night), rs.get(night), ev.get(night)) if x]
        start = min(x["start"] for x in parts)
        stop = max(x["stop"] for x in parts)
        midnight = _local_midnight(night + dt.timedelta(days=1), tz)
        cut = None
        if (not keep_dst_evening and not isinstance(tz, int)
                and midnight - _local_midnight(night, tz) != 86400 and start < midnight):
            cut, start = midnight, max(start, midnight)
        rows = []                                    # (time, order, line)
        if night in br:
            for t0, t1, sev in br[night]["periods"]:
                if cut is not None:
                    if t1 <= cut:
                        continue
                    t0 = max(t0, cut)
                v = SOMNOPOSE_VALUE.get(sev, 0)
                # at a shared timestamp the end of a period comes before the next start
                rows += [(t0, 2, f"{_somno_t(t0)},{v},"), (t1, 0, f"{_somno_t(t1)},{v},")]
        if night in ev and ev[night].get("position"):
            # watch on the chest: Inclination = body roll angle (0 back, +90 left,
            # -90 right, +-180 belly)
            for t, roll, _, _ in ev[night]["position"]:
                rows.append((t, 1, f"{_somno_t(t)},,{int(round(roll))}"))
        elif night in ev:
            line = lambda t, v: (t, 1, f"{_somno_t(t)},,{v}")
            rows += [line(start, 0), line(stop, 0)]
            for e in ev[night]["events"]:
                if e["start"] < start:
                    continue
                if e["type"] == "awakening":
                    rows += [line(e["start"] - 1, 0), line(e["start"], 2), line(e["end"], 2), line(e["end"] + 1, 0)]
                else:
                    t = (e["start"] + e["end"]) // 2
                    rows += [line(t - 30, 0), line(t, 1), line(t + 30, 0)]
        elif night in rs:
            pts = [(start, 0)] + [(m, 1) for m in rs[night]["moments"] if m >= start] + [(stop, 0)]
            for t, v in pts:
                if v:
                    rows += [(t - 30, 1, f"{_somno_t(t - 30)},,0"),
                             (t, 1, f"{_somno_t(t)},,1"),
                             (t + 30, 1, f"{_somno_t(t + 30)},,0")]
                else:
                    rows.append((t, 1, f"{_somno_t(t)},,0"))
        if not rows:
            continue
        rows.sort(key=lambda r: (r[0], r[1]))
        blocks.append([r[2] for r in rows])
    if not blocks:
        return None
    key = lambda b: str(night_of(int(float(b[0].split(",")[0])) + SOMNOPOSE_EPOCH - CLOCK_OFFSET, tz))
    report = save_blocks(path, "Timestamp,Orientation,Inclination", blocks, key,
                         overwrite=overwrite, log=log)
    log(f"-> {path}: {report} (Orientation = breathing disruptions, "
        "Inclination = restless moments, or awakenings on night-app nights)")
    return path


SUMMARY_HEADER = ("Night,Sleep start,Sleep end,Sleep score,Sleep quality,Restlessness,Interruptions,"
                  "Awakenings,Restless moments,Deep (min),Light (min),REM (min),Awake (min),"
                  "SpO2 avg,SpO2 min,SpO2 under 90% (min),Breathing overall,"
                  "Breathing low (min),Breathing medium (min),Breathing high (min),"
                  "Steps (day before),Distance km (day before),Active min (day before),"
                  "Active kcal (day before),Intensity min (day before),Activities (day before),"
                  "Micro-awakenings (movement),Awakenings (movement),Awake min (movement),"
                  "Back (min),Left (min),Right (min),Belly (min),Upright (min),Position changes")


def write_summary_csv(path, readings, stages, breathing, restless, tz, log=print, overwrite=False,
                      days=None, night_recs=None):
    """One row per night with what OSCAR can't show: Garmin's scores, counts,
    stage minutes, SpO2 summary and breathing disruptions. Scores come from
    the .fit file when available (numbers), else from Garmin Connect (ratings)."""
    st = {}
    for sess in sorted(stages, key=lambda x: (x["start"], "quality" not in (x.get("scores") or {}))):
        n = night_of(sess["start"], tz)
        cur = st.get(n)
        if cur is None:
            st[n] = dict(sess, scores=dict(sess.get("scores") or {}))
        else:                                        # merge scores; .fit numbers win
            for k, v in (sess.get("scores") or {}).items():
                if not isinstance(cur["scores"].get(k), (int, float)):
                    cur["scores"][k] = v
    br, rs = _dedupe_by_night(breathing, tz), _dedupe_by_night(restless, tz)
    spo2 = {}
    for t, v, _ in readings:
        spo2.setdefault(night_of(t, tz), []).append(v)
    spo2 = {n: v for n, v in spo2.items() if len(v) >= 30}
    days = days or {}
    today = dt.date.today()
    day_nights = {dt.date.fromisoformat(k) for k in days if dt.date.fromisoformat(k) < today}
    nr = _dedupe_by_night(night_recs or [], tz)
    nights = sorted(set(st) | set(br) | set(rs) | set(spo2) | day_nights | set(nr))
    if not nights:
        return None
    out_rows = []
    for n in nights:
        s_, b_, r_, o_ = st.get(n), br.get(n), rs.get(n), spo2.get(n)
        src = s_ or b_ or r_
        sc = (s_ or {}).get("scores") or {}
        mins = {"Deep": 0, "Light": 0, "REM": 0, "WAKE": 0}
        for t0, t1, stage in (s_ or {}).get("intervals", []):
            if stage in mins:
                mins[stage] += (t1 - t0) / 60
        bm = {"low": 0, "medium": 0, "high": 0}
        for t0, t1, sev in (b_ or {}).get("periods", []):
            if sev in bm:
                bm[sev] += (t1 - t0) / 60
        cells = [
            str(n),
            f"{to_local(src['start'], tz):%Y-%m-%d %H:%M}" if src else "",
            f"{to_local(src['stop'], tz):%Y-%m-%d %H:%M}" if src else "",
            str(sc.get("overall", "")), str(sc.get("quality", "")),
            str(sc.get("restlessness", "")), str(sc.get("interruptions", "")),
            "" if not s_ or s_.get("awakenings") is None else str(s_["awakenings"]),
            str(r_["count"]) if r_ else "",
        ]
        cells += [f"{mins[k]:.0f}" if s_ else "" for k in ("Deep", "Light", "REM", "WAKE")]
        cells += ([f"{sum(o_) / len(o_):.1f}", str(min(o_)), str(sum(1 for v in o_ if v < 90))]
                  if o_ else ["", "", ""])
        cells += ([b_.get("overall") or ""] + [f"{bm[k]:.0f}" for k in ("low", "medium", "high")]
                  if b_ else ["", "", "", ""])
        cells += day_before_cells(days.get(str(n)))
        if n in nr:
            sm = nr[n]["summary"]
            cells += [str(sm["micro"]), str(len(sm["awakenings"])), f"{sm['awake_min']:.0f}"]
        else:
            cells += ["", "", ""]
        ps = nr[n]["summary"].get("position") if n in nr else None
        cells += ([f"{ps['mins'][p]:.0f}" for p in ("back", "left", "right", "belly", "upright")]
                  + [str(ps["changes"])] if ps else ["", "", "", "", "", ""])
        out_rows.append(",".join(cells))
    report = save_rows(path, SUMMARY_HEADER, out_rows, key=lambda r: r.split(",")[0],
                       overwrite=overwrite, log=log)
    log(f"-> {path}: {report}")
    return path


def write_breathing_csv(outdir, sessions, tz, log=print, overwrite=False):
    """Writes breathing/garmin_breathing_summary.csv (one row per night) and
    breathing/garmin_breathing_periods.csv (one row per period)."""
    uniq = []
    for sess in sorted(sessions, key=lambda x: x["start"]):
        if uniq and abs(sess["start"] - uniq[-1]["start"]) < 1800:
            continue
        uniq.append(sess)
    if not uniq:
        return []
    folder = os.path.join(outdir, "breathing")
    os.makedirs(folder, exist_ok=True)
    levels = ["none", "low", "medium", "high", "not measured"]
    summary = os.path.join(folder, "garmin_breathing_summary.csv")
    periods = os.path.join(folder, "garmin_breathing_periods.csv")
    sum_rows, per_rows = [], []
    for sess in uniq:
        start = to_local(sess["start"], tz)
        night = (start - dt.timedelta(hours=12)).date() if start.hour < 12 else start.date()
        mins = {k: 0.0 for k in levels}
        for t0, t1, sev in sess["periods"]:
            mins[sev] += (t1 - t0) / 60
            per_rows.append(f"{night},{to_local(t0, tz):%Y-%m-%d %H:%M:%S},"
                            f"{to_local(t1, tz):%Y-%m-%d %H:%M:%S},{(t1 - t0) / 60:.0f},{sev}")
        sum_rows.append(f"{night},{start:%Y-%m-%d %H:%M:%S},{to_local(sess['stop'], tz):%Y-%m-%d %H:%M:%S},"
                        f"{sess.get('overall') or ''},"
                        + ",".join(f"{mins[k]:.0f}" for k in levels))
        log(f"Breathing disruptions, night of {night}: overall {sess.get('overall') or 'n/a'}, "
            f"low {mins['low']:.0f} min, medium {mins['medium']:.0f} min, high {mins['high']:.0f} min")
    first_col = lambda r: r.split(",")[0]
    r1 = save_rows(summary, "Night,Start,End,Overall,None (min),Low (min),Medium (min),High (min),"
                   "Not measured (min)", sum_rows, first_col, overwrite, log)
    r2 = save_rows(periods, "Night,Start,End,Minutes,Severity", per_rows, first_col, overwrite, log)
    log(f"-> {summary}: {r1}\n-> {periods}: {r2}")
    return [summary, periods]


# ---------------------------------------------------------------------------
# Cumulative files: a new run replaces the nights it covers and keeps the
# others, so running the script every day builds up a history.
# ---------------------------------------------------------------------------
def _night_from_text(text):
    """'2026-10-18T23:41:45+02:00' or '2026-10-18 23:41' -> '2026-10-18' (noon split)."""
    t = dt.datetime.fromisoformat(text.strip().replace(" ", "T"))
    return str(t.date() - dt.timedelta(days=1) if t.hour < 12 else t.date())


def _safe_key(key):
    def k(item):
        try:
            return key(item)
        except Exception:
            return ""
    return k


def _merge(path, header, items, key, overwrite, read_items, write_items, log):
    """Shared logic for save_rows / save_blocks. Returns a short report."""
    key = _safe_key(key)
    new_keys = {key(i) for i in items}
    old_items = []
    if not overwrite and os.path.exists(path):
        with open(path) as f:
            text = f.read()
        lines = text.splitlines()
        if lines and lines[0] == header:
            old_items = read_items(lines[1:])
        elif lines:
            backup = path + ".old"
            os.replace(path, backup)
            log(f"   (different format: the previous file was kept as {backup})")
    old_keys = {key(i) for i in old_items}
    kept = [i for i in old_items if key(i) not in new_keys]
    merged = sorted(kept + items, key=key)            # stable: keeps order within a night
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(header + "\n" + write_items(merged))
    added, updated = len(new_keys - old_keys), len(new_keys & old_keys)
    total = len({key(i) for i in merged})
    return f"{added} night(s) added, {updated} updated, {total} in total"


def save_rows(path, header, rows, key, overwrite=False, log=print):
    """Writes CSV rows (strings), keeping the rows of other nights already in the file."""
    return _merge(path, header, rows, key, overwrite,
                  read_items=lambda lines: [l for l in lines if l.strip()],
                  write_items=lambda items: "".join(r + "\n" for r in items), log=log)


def save_blocks(path, header, blocks, key, overwrite=False, log=print):
    """Same for Somnopose files, where each night is a block of lines ending with '-'."""
    def read_blocks(lines):
        out, cur = [], []
        for l in lines:
            if l.strip() == "-":
                if cur:
                    out.append(cur)
                cur = []
            elif l.strip():
                cur.append(l)
        if cur:
            out.append(cur)
        return out
    return _merge(path, header, blocks, key, overwrite, read_items=read_blocks,
                  write_items=lambda items: "".join("\n".join(b) + "\n-\n" for b in items), log=log)


# ---------------------------------------------------------------------------
# Activity of the day before each night (steps, distance, active time...)
# ---------------------------------------------------------------------------
STEP_TYPES = {0, 1, 6}          # generic, running, walking: "cycles" are steps


def extract_daily_activity(path, tz):
    """Daily totals from a WELLNESS .fit file.

    The watch keeps running totals per activity type that restart at
    midnight; the message stamped at midnight (duration_min = 1440) closes
    the day. Returns {"YYYY-MM-DD": {"types": {type: {field: max}}, "complete": bool}}.
    """
    days, mon_ts = {}, None
    for gnum, v in parse_fit(path):
        if gnum != MSG_MONITORING:
            continue
        if 253 in v and 26 not in v:
            mon_ts = v[253]
        t = v.get(253)
        if 26 in v and mon_ts is not None:
            mon_ts = mon_ts + ((v[26] - mon_ts) & 0xFFFF)
            t = mon_ts
        if t is None:
            continue
        atype = v[5] if 5 in v else (v[24] & 0x1F if 24 in v else None)
        if atype is None:
            continue
        day = str(to_local(t - 1, tz).date())      # a midnight stamp closes the previous day
        d = days.setdefault(day, {"types": {}, "complete": False})
        if v.get(29) == 1440:
            d["complete"] = True
        tot = d["types"].setdefault(atype, {})
        for field, key, scale in ((3, "cycles", 1), (2, "distance", 100), (4, "active_time", 1000),
                                  (19, "active_kcal", 1)):
            if field in v:
                tot[key] = max(tot.get(key, 0), v[field] / scale)
    return days


def merge_daily(target, days):
    for day, d in days.items():
        cur = target.setdefault(day, {"types": {}, "complete": False})
        cur["complete"] = cur["complete"] or d["complete"]
        for atype, tot in d["types"].items():
            ct = cur["types"].setdefault(atype, {})
            for k, v in tot.items():
                ct[k] = max(ct.get(k, 0), v)
    return target


def daily_totals_from_fit(merged):
    """{day: totals} for the complete days only."""
    out = {}
    for day, d in merged.items():
        if not d["complete"]:
            continue
        types = d["types"].values()
        out[day] = {
            "steps": int(sum(t.get("cycles", 0) for a, t in d["types"].items() if a in STEP_TYPES)),
            "distance_km": sum(t.get("distance", 0) for t in types) / 1000,
            "active_min": sum(t.get("active_time", 0) for t in types) / 60,
            "active_kcal": sum(t.get("active_kcal", 0) for t in types),
        }
    return out


def daily_totals_from_json(stats, activities):
    """Day totals from Garmin Connect's daily summary and activity list."""
    stats = stats or {}
    out = {}
    num = lambda k: stats.get(k) if isinstance(stats.get(k), (int, float)) and not isinstance(stats.get(k), bool) else None
    if num("totalSteps") is not None:
        out["steps"] = int(num("totalSteps"))
    if num("totalDistanceMeters") is not None:
        out["distance_km"] = num("totalDistanceMeters") / 1000
    secs = (num("activeSeconds") or 0) + (num("highlyActiveSeconds") or 0)
    if secs:
        out["active_min"] = secs / 60
    if num("activeKilocalories") is not None:
        out["active_kcal"] = num("activeKilocalories")
    if num("moderateIntensityMinutes") is not None or num("vigorousIntensityMinutes") is not None:
        out["moderate_min"] = num("moderateIntensityMinutes") or 0
        out["vigorous_min"] = num("vigorousIntensityMinutes") or 0
    if num("floorsAscended") is not None:
        out["floors"] = num("floorsAscended")
    if num("averageStressLevel") is not None and num("averageStressLevel") >= 0:
        out["stress"] = num("averageStressLevel")
    if num("bodyBatteryChargedValue") is not None:
        out["bb_charged"] = num("bodyBatteryChargedValue")
    if num("bodyBatteryDrainedValue") is not None:
        out["bb_drained"] = num("bodyBatteryDrainedValue")
    acts = []
    for a in activities or []:
        name = a.get("activityName") or ((a.get("activityType") or {}).get("typeKey") or "activity")
        dur = a.get("duration")
        dist = a.get("distance")
        acts.append((" ".join(str(name).replace(",", " ").split()), (dur or 0) / 60, (dist or 0) / 1000))
    if acts:
        out["activities"] = acts
    return out


def day_before_text(d):
    """'4,730 steps, 3.4 km, ...' for the Journal note."""
    parts = []
    if "steps" in d:
        parts.append(f"{d['steps']:,} steps")
    if d.get("distance_km"):
        parts.append(f"{d['distance_km']:.1f} km")
    if d.get("active_min"):
        parts.append(f"{_dur(d['active_min'])} active")
    if d.get("active_kcal"):
        parts.append(f"{d['active_kcal']:.0f} active kcal")
    if "moderate_min" in d:
        total = d["moderate_min"] + 2 * d["vigorous_min"]
        parts.append(f"{total:.0f} intensity min ({d['moderate_min']:.0f} moderate, "
                     f"{d['vigorous_min']:.0f} vigorous)")
    if d.get("floors"):
        parts.append(f"{d['floors']:.0f} floors")
    if "stress" in d:
        parts.append(f"stress {d['stress']:.0f}")
    if "bb_charged" in d or "bb_drained" in d:
        parts.append(f"Body Battery +{d.get('bb_charged', 0):.0f}/-{d.get('bb_drained', 0):.0f}")
    if d.get("activities"):
        parts.append("activities: " + "; ".join(
            f"{n} {_dur(m)}" + (f" {k:.1f} km" if k >= 0.05 else "") for n, m, k in d["activities"]))
    return ", ".join(parts)


DAY_COLUMNS = ("Steps (day before),Distance km (day before),Active min (day before),"
               "Active kcal (day before),Intensity min (day before),Activities (day before)")


def day_before_cells(d):
    if not d:
        return ["", "", "", "", "", ""]
    return [str(d["steps"]) if "steps" in d else "",
            f"{d['distance_km']:.2f}" if "distance_km" in d else "",
            f"{d['active_min']:.0f}" if "active_min" in d else "",
            f"{d['active_kcal']:.0f}" if "active_kcal" in d else "",
            f"{d['moderate_min'] + 2 * d['vigorous_min']:.0f}" if "moderate_min" in d else "",
            "; ".join(f"{n} {m:.0f} min" for n, m, _ in d.get("activities", []))]


# ---------------------------------------------------------------------------
# Night recording made by an activity app (1 s SpO2 / pulse, 100 Hz motion)
# ---------------------------------------------------------------------------
NIGHT_SPO2_NAMES = ("spo2", "oxygen", "saturation")
NIGHT_HR_NAMES = ("hr", "heart", "pulse")
_FMT = {0x00: "B", 0x01: "b", 0x02: "B", 0x83: "h", 0x84: "H", 0x85: "i", 0x86: "I",
        0x88: "f", 0x89: "d", 0x0A: "B", 0x8B: "H", 0x8C: "I", 0x0D: "B"}
_SIZE = {"B": 1, "b": 1, "h": 2, "H": 2, "i": 4, "I": 4, "f": 4, "d": 8}


def extract_night_recording(path):
    """Reads an activity .fit recorded during the night by an app that logs
    SpO2 and pulse every second (developer fields) and the accelerometer.

    Returns {"start", "stop", "spo2": {t: %}, "hr": {t: bpm}, "act": {t: motion}}
    (FIT UTC seconds), or None when the file holds no such data. "act" is the
    sum of the sample-to-sample changes of the 3 axes over each second.
    """
    data = open(path, "rb").read()
    if len(data) < 12 or data[8:12] != b".FIT":
        return None
    hsize = data[0]
    end = min(hsize + struct.unpack_from("<I", data, 4)[0], len(data))
    pos, defs, dev_desc = hsize, {}, {}
    last_ts = None
    spo2, hr, act = {}, {}, {}
    grav = {}            # second -> [sum x, sum y, sum z, n] (calibrated accelerometer only)
    prev = None
    while pos < end:
        rh = data[pos]
        pos += 1
        if rh & 0x80:                                   # compressed timestamp
            d = defs[(rh >> 5) & 3]
            if last_ts is not None:
                off = rh & 0x1F
                last_ts = last_ts + ((off - (last_ts & 0x1F)) & 0x1F)
            ts = last_ts
        elif rh & 0x40:                                 # definition
            e = "<" if data[pos + 1] == 0 else ">"
            g = struct.unpack_from(e + "H", data, pos + 2)[0]
            nf = data[pos + 4]
            pos += 5
            fields, o = {}, 0
            for i in range(nf):
                num, size, bt = data[pos], data[pos + 1], data[pos + 2]
                fields[num] = (o, size, bt)
                o += size
                pos += 3
            devs = []
            if rh & 0x20:
                nd = data[pos]
                pos += 1
                for i in range(nd):
                    devs.append((data[pos], data[pos + 1], data[pos + 2], o))
                    o += data[pos + 1]
                    pos += 3
            defs[rh & 0x0F] = {"g": g, "e": e, "f": fields, "dev": devs, "size": o}
            continue
        else:
            d = defs[rh & 0x0F]
            ts = None
        g, e, f = d["g"], d["e"], d["f"]
        if 253 in f:
            ts = struct.unpack_from(e + "I", data, pos + f[253][0])[0]
            last_ts = ts
        if g == 206:                                    # field_description
            def u8(n):
                return data[pos + f[n][0]] if n in f else None
            name = b""
            if 3 in f:
                o, size, _ = f[3]
                name = data[pos + o:pos + o + size].split(b"\0")[0]
            dev_desc[(u8(0), u8(1))] = (name.decode("utf-8", "replace").lower(), u8(2))
        elif g == 20 and ts is not None:                # record
            for num, size, idx, o in d["dev"]:
                name, bt = dev_desc.get((idx, num), ("", 2))
                fmt = _FMT.get(bt, "B")
                if _SIZE[fmt] != size:
                    continue
                v = struct.unpack_from(e + fmt, data, pos + o)[0]
                if any(k in name for k in NIGHT_SPO2_NAMES):
                    if 50 <= v <= 100:
                        spo2[ts] = v
                elif any(k in name for k in NIGHT_HR_NAMES):
                    if 25 <= v <= 250:
                        hr[ts] = v
            if ts not in hr and 3 in f and f[3][1] == 1:
                v = data[pos + f[3][0]]
                if 25 <= v <= 250:
                    hr[ts] = v
        elif g == 165 and ts is not None:               # accelerometer_data
            axes = [f.get(5), f.get(6), f.get(7)] if 5 in f else [f.get(2), f.get(3), f.get(4)]
            if all(axes):
                fmt = "f" if 5 in f else "H"
                n = axes[0][1] // _SIZE[fmt]
                xs, ys, zs = (struct.unpack_from(e + str(n) + fmt, data, pos + a[0]) for a in axes)
                total = 0.0
                for i in range(n):
                    if prev is not None:
                        total += abs(xs[i] - prev[0]) + abs(ys[i] - prev[1]) + abs(zs[i] - prev[2])
                    prev = (xs[i], ys[i], zs[i])
                act[ts] = act.get(ts, 0.0) + total
                if fmt == "f":
                    gsum = grav.setdefault(ts, [0.0, 0.0, 0.0, 0])
                    gsum[0] += sum(xs)
                    gsum[1] += sum(ys)
                    gsum[2] += sum(zs)
                    gsum[3] += n
        pos += d["size"]
    if not spo2 and not act:
        return None
    times = sorted(set(spo2) | set(hr) | set(act))
    grav = {t: (g[0] / g[3], g[1] / g[3], g[2] / g[3]) for t, g in grav.items() if g[3]}
    return {"start": times[0], "stop": times[-1], "spo2": spo2, "hr": hr, "act": act, "grav": grav}


# --- Body position from a watch taped on the chest ---------------------------
# Frame (watch on the chest, screen out): the accelerometer gives the direction
# of gravity; y points to the head, z out of the chest, x to the left. Lying on
# the back -> z ~ -1 g, standing -> y ~ -1 g. --chest-flip if the watch is
# upside down (left / right swapped).
POSITIONS = ["belly", "left", "back", "right", "upright"]
SIDE_DEG, BELLY_DEG, UPRIGHT_DEG = 45, 135, 45


def body_position(grav, flip=False):
    """[(t, roll, elevation, position)] per window from per-second gravity.

    roll: rotation around the head-feet axis, 0 = back, +90 = left side,
    -90 = right side, +-180 = belly. elevation: 90 = upright. Windows of 10 s
    (1 s for recordings shorter than 30 min), median of the seconds, then a
    majority filter over 1 min (5 s) removes very short changes.
    """
    import math
    if not grav:
        return [], 1
    times = sorted(grav)
    short = times[-1] - times[0] < 1800
    win, smooth = (1, 5) if short else (10, 6)
    bins = {}
    for t in times:
        bins.setdefault(t - t % win, []).append(grav[t])
    sx = -1 if flip else 1
    out = []
    for t in sorted(bins):
        vals = bins[t]
        med = [sorted(v[i] for v in vals)[len(vals) // 2] for i in range(3)]
        x, y, z = med
        norm = math.sqrt(x * x + y * y + z * z) or 1.0
        roll = math.degrees(math.atan2(sx * x, -z))
        elev = math.degrees(math.asin(max(-1.0, min(1.0, -sx * y / norm))))
        if elev > UPRIGHT_DEG:
            pos = "upright"
        elif abs(roll) > BELLY_DEG:
            pos = "belly"
        elif roll > SIDE_DEG:
            pos = "left"
        elif roll < -SIDE_DEG:
            pos = "right"
        else:
            pos = "back"
        out.append([t, roll, elev, POSITIONS.index(pos)])
    codes = [o[3] for o in out]
    for i, o in enumerate(out):
        w = codes[max(0, i - smooth // 2):i + (smooth - 1) // 2 + 1]
        o[3] = POSITIONS[max(range(len(POSITIONS)), key=lambda c: (w.count(c), -c))]
    return [tuple(o) for o in out], win


def position_summary(pos, win):
    mins = {p: 0.0 for p in POSITIONS}
    for _, _, _, p in pos:
        mins[p] += win / 60
    lying = [p for _, _, _, p in pos if p != "upright"]
    changes = sum(1 for i in range(1, len(lying)) if lying[i] != lying[i - 1])
    return {"mins": mins, "changes": changes}


def analyze_movement(act, hr):
    """Movement index per second and movement-based (micro-)awakenings.

    The quiet level is the median of the night; a second "moves" when it is
    40 % above it. Moving seconds less than 4 s apart form an episode; 1-2 s
    twitches without a pulse rise of 8 bpm are ignored. Episodes less than
    60 s apart form a bout: an awakening when it holds 30 s of movement or
    lasts 2 min, otherwise a micro-awakening. This is an actigraphy estimate,
    not an EEG arousal count.
    """
    if not act:
        return {}, []
    vals = sorted(act.values())
    med = vals[len(vals) // 2] or 1.0
    index = {t: max(0.0, v - med) for t, v in act.items()}
    moving = sorted(t for t, v in act.items() if v > 1.4 * med)

    def hr_rise(a, b):
        base = [hr[t] for t in range(a - 30, a) if t in hr]
        peak = [hr[t] for t in range(a, b + 21) if t in hr]
        return (max(peak) - sum(base) / len(base)) if base and peak else 0.0

    episodes = []
    for t in moving:
        if episodes and t - episodes[-1][1] <= 3:
            episodes[-1][1] = t
            episodes[-1][2] += 1
        else:
            episodes.append([t, t, 1])
    episodes = [ep for ep in episodes if ep[2] >= 3 or hr_rise(ep[0], ep[1]) >= 8]
    bouts = []
    for a, b, n in episodes:
        if bouts and a - bouts[-1]["end"] <= 60:
            bouts[-1]["end"] = b
            bouts[-1]["moving"] += n
        else:
            bouts.append({"start": a, "end": b, "moving": n})
    # handling the watch to start / stop the app is not an awakening
    first, last = min(act), max(act)
    bouts = [bt for bt in bouts if bt["start"] - first > 120 and last - bt["end"] > 120]
    for bt in bouts:
        bt["type"] = "awakening" if bt["moving"] >= 30 or bt["end"] - bt["start"] >= 120 else "micro-awakening"
        bt["hr_rise"] = hr_rise(bt["start"], bt["end"])
    return index, bouts


def night_recording_summary(rec, events):
    hrs = list(rec["hr"].values())
    aw = [e for e in events if e["type"] == "awakening"]
    return {"micro": sum(1 for e in events if e["type"] == "micro-awakening"),
            "awakenings": [(e["start"], e["end"]) for e in aw],
            "awake_min": sum(e["end"] - e["start"] + 1 for e in aw) / 60,
            "hr": (sum(hrs) / len(hrs), min(hrs), max(hrs)) if hrs else None,
            "start": rec["start"], "stop": rec["stop"]}


def night_minute_readings(rec):
    """SpO2 of the night recording, one value per minute (for the summary and Journal)."""
    out = {}
    for t, v in rec["spo2"].items():
        out.setdefault(t - t % 60, (t - t % 60, v, 0))
    return sorted(out.values())


def write_viatom_night(path, rec, index, tz, step=2, start=None, stop=None, extra_spo2=None, extra_hr=None):
    """Viatom file from a night recording: SpO2 and pulse every 2 s, and the
    movement index (max over the 2 s, / 100, capped at 255) in the Movement graph.
    start / stop / extra_*: to cover the whole night in the same file, with the
    watch's own SpO2 and pulse ({second: value}) before and after the recording."""
    start = rec["start"] if start is None else start
    stop = rec["stop"] if stop is None else stop
    extra_spo2, extra_hr = extra_spo2 or {}, extra_hr or {}
    n = min((stop - start) // step + 1, 65535 // step)
    if n <= 0:
        return None
    recs = bytearray()
    o2s, prs = [], []
    last_o2 = None
    for k in range(n):
        t = start + k * step
        if rec["start"] <= t <= rec["stop"]:
            o2 = next((rec["spo2"][x] for x in range(t, t + step) if x in rec["spo2"]), last_o2)
            last_o2 = o2 if o2 is not None else last_o2
            pr = next((rec["hr"][x] for x in range(t, t + step) if x in rec["hr"]), None)
        else:
            o2 = next((extra_spo2[x] for x in range(t, t + step) if x in extra_spo2), None)
            pr = next((extra_hr[x] for x in range(t, t + step) if x in extra_hr), None)
        mv = max((index.get(x, 0.0) for x in range(t, t + step)), default=0.0)
        mv = max(0, min(255, int(round(mv / 100))))
        if pr is not None and 30 <= pr <= 250:
            if o2 is not None and 61 <= o2 <= 100:
                o2s.append(o2)
            else:
                o2 = 0xFF
            prs.append(pr)
            recs += bytes([o2, pr, 0, mv, 0])
        else:
            recs += bytes([0xFF, 0xFF, 0xFF, mv, 0])
    lt = to_local(start, tz)
    size = VIATOM_HEADER + len(recs)
    hdr = bytearray(VIATOM_HEADER)
    struct.pack_into("<HHBBBBB", hdr, 0, 5, lt.year, lt.month, lt.day, lt.hour, lt.minute, lt.second)
    hdr[9:12] = size.to_bytes(3, "little")
    struct.pack_into("<H", hdr, 13, n * step)
    if o2s:
        hdr[17] = round(sum(o2s) / len(o2s))
        hdr[18] = min(o2s)
        struct.pack_into("<H", hdr, 22, min(65535, step * sum(x < 90 for x in o2s)))
    if prs:
        hdr[30] = min(255, round(sum(prs) / len(prs)))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(hdr + recs)
    return path


def write_night_csv(path, rec, index, events, tz):
    """Second-by-second CSV of the night recording, and the list of awakenings."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        fh.write("Time,SpO2,Pulse,Movement\n")
        for t in range(rec["start"], rec["stop"] + 1):
            if t in rec["spo2"] or t in rec["hr"] or t in index:
                fh.write(f"{to_local(t, tz):%Y-%m-%d %H:%M:%S},{rec['spo2'].get(t, '')},"
                         f"{rec['hr'].get(t, '')},{index.get(t, 0):.0f}\n")
    ev_path = path[:-4] + "_awakenings.csv"
    with open(ev_path, "w") as fh:
        fh.write("Type,Start,End,Duration (s),Movement (s),Pulse rise (bpm)\n")
        for e in events:
            fh.write(f"{e['type']},{to_local(e['start'], tz):%Y-%m-%d %H:%M:%S},"
                     f"{to_local(e['end'], tz):%Y-%m-%d %H:%M:%S},{e['end'] - e['start'] + 1},"
                     f"{e['moving']},{e['hr_rise']:.0f}\n")
    return path, ev_path


def merge_watch_spo2(rec, readings, max_hold=120):
    """Uses the watch's own SpO2 (1 reading per minute) instead of the app's.

    An app reading the watch's SpO2 every second gets a value that changes
    less often and about 10 min late; the watch's readings are the reference.
    Each reading is held until the next one (at most max_hold seconds).
    The app's pulse and movement are kept. Unchanged if the watch has fewer
    than 30 readings during the recording.
    """
    rs = sorted(r for r in readings if rec["start"] - 60 <= r[0] <= rec["stop"])
    if len(rs) < 30:
        return rec
    spo2 = {}
    for i, (t, v, _) in enumerate(rs):
        nxt = rs[i + 1][0] if i + 1 < len(rs) else t + 60
        for sec in range(max(t, rec["start"]), min(nxt, t + max_hold, rec["stop"] + 1)):
            spo2[sec] = v
    return dict(rec, spo2=spo2, spo2_source="watch")


def handle_night_recording(rec, tz, outdir, write_csv=True, keep_dst_evening=False, log=print,
                           watch_readings=None, watch_hr=None, chest=False, chest_flip=False):
    """Viatom file and CSVs for one night recording. Returns (night_rec, minute_readings)
    where night_rec = {"start", "stop", "events", "summary"}. watch_readings: the
    watch's own SpO2 readings, used instead of the app's when they cover the night."""
    if watch_readings:
        rec = merge_watch_spo2(rec, watch_readings)
    index, events = analyze_movement(rec["act"], rec["hr"])
    n = night_of(rec["start"], tz)
    start = rec["start"]
    if not keep_dst_evening and not isinstance(tz, int):
        midnight = _local_midnight(n + dt.timedelta(days=1), tz)
        if midnight - _local_midnight(n, tz) != 86400 and start < midnight:
            start = midnight          # same OSCAR time-axis issue as for the other files
    # the watch's own readings of the same night (before / after the recording)
    # go into the same file: one oximetry session per night in OSCAR
    lower = start if start > rec["start"] else 0       # only a DST-evening cut limits them
    outside = sorted(r for r in (watch_readings or [])
                     if night_of(r[0], tz) == n and r[0] >= lower
                     and not (rec["start"] <= r[0] <= rec["stop"]))
    span_start, span_stop = start, rec["stop"]
    extra_spo2, extra_hr = {}, {}
    if outside:
        s0, s1 = min(start, outside[0][0]), max(rec["stop"], outside[-1][0] + 59)
        if s1 - s0 < 18 * 3600:
            span_start, span_stop = s0, s1
            hr_times = sorted(watch_hr or {})
            for i, (t, v, _) in enumerate(outside):
                nxt = outside[i + 1][0] if i + 1 < len(outside) else t + 60
                pr = nearest_hr(hr_times, watch_hr or {}, t, tol=300)
                for sec in range(t, min(nxt, t + 120)):
                    if not (rec["start"] <= sec <= rec["stop"]):
                        extra_spo2[sec] = v
                        if pr:
                            extra_hr[sec] = pr
    lt0 = to_local(span_start, tz)
    stamp = f"{lt0:%Y%m%d%H%M%S}"
    vpath = write_viatom_night(os.path.join(outdir, "oscar_viatom", f"Garmin_{stamp}"), rec, index, tz,
                               start=span_start, stop=span_stop, extra_spo2=extra_spo2, extra_hr=extra_hr)
    if write_csv:
        write_night_csv(os.path.join(outdir, f"garmin_night_{stamp}.csv"), rec, index, events, tz)
    summary = night_recording_summary(rec, events)
    summary["start"] = start
    position = None
    if chest and rec.get("grav"):
        pos, win = body_position(rec["grav"], chest_flip)
        position = [p for p in pos if p[0] >= start]
        if position:
            summary["position"] = position_summary(position, win)
            if write_csv:
                with open(os.path.join(outdir, f"garmin_night_{stamp}_position.csv"), "w") as fh:
                    fh.write("Time,Roll (deg),Elevation (deg),Position\n")
                    for t, r, e, p in position:
                        fh.write(f"{to_local(t, tz):%Y-%m-%d %H:%M:%S},{int(round(r))},{int(round(e))},{p}\n")
    aw = summary["awakenings"]
    log(f"-> {vpath}: night {lt0:%Y-%m-%d %H:%M} to {to_local(span_stop, tz):%H:%M}, recording "
        f"{to_local(start, tz):%H:%M}-{to_local(rec['stop'], tz):%H:%M}, "
        f"SpO2 ({'watch, 1/min' if rec.get('spo2_source') == 'watch' else 'app'}) + pulse every 2 s, "
        f"movement; {summary['micro']} micro-awakenings, {len(aw)} awakenings")
    if position:
        m = summary["position"]["mins"]
        log("   body position (watch on the chest): " + ", ".join(
            f"{p} {_dur(m[p])}" for p in ("back", "left", "right", "belly", "upright") if m[p] >= 0.5)
            + f", {summary['position']['changes']} changes")
    return ({"start": start, "stop": rec["stop"], "events": events, "summary": summary,
             "position": position},
            [r for r in night_minute_readings(rec) if r[0] >= start])


# ---------------------------------------------------------------------------
# Other overnight values (HRV, skin temperature, respiration...) for the
# OSCAR Journal notes
# ---------------------------------------------------------------------------
def extract_extras(path):
    """Overnight values from HRV_STATUS / SKIN_TEMP .fit files.

    Returns [{"t": timestamp, ...}] where t is stamped at wake-up (so it
    belongs to the previous evening's night).
    """
    out = []
    for gnum, v in parse_fit(path):
        if gnum == 370 and 253 in v:               # hrv_status_summary
            e = {"t": v[253]}
            if 1 in v:
                e["hrv"] = v[1] / 128
            if 0 in v:
                e["hrv_weekly"] = v[0] / 128
            if 6 in v:
                e["hrv_status"] = HRV_STATUS.get(v[6])
            out.append(e)
        elif gnum == 398 and 253 in v and 1 in v:  # skin_temp_overnight
            out.append({"t": v[253], "skin_temp": v[1]})
    return out


def extract_respiration(path):
    """Breathing rate per minute from WELLNESS .fit files: {timestamp: breaths/min}."""
    return {v[253]: v[0] / 100 for gnum, v in parse_fit(path)
            if gnum == 297 and 253 in v and 0 in v and v[0] > 0}


def extras_from_json(data, to_fit_ts):
    """The same values from Garmin Connect sleep JSON (dailySleepDTO and around)."""
    data = data or {}
    dto = data.get("dailySleepDTO") or {}
    t = to_fit_ts(dto.get("sleepEndTimestampGMT")) or to_fit_ts(dto.get("sleepStartTimestampGMT"))
    if t is None:
        return []
    e = {"t": t}
    pairs = [("hrv", data.get("avgOvernightHrv")), ("rhr", data.get("restingHeartRate")),
             ("skin_temp", data.get("avgSkinTempDeviationC")),
             ("resp", dto.get("averageRespirationValue")), ("body_battery", data.get("bodyBatteryChange")),
             ("stress", dto.get("avgSleepStress")), ("avg_hr", dto.get("avgHeartRate"))]
    for k, v in pairs:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            e[k] = v
    if isinstance(data.get("hrvStatus"), str):
        e["hrv_status"] = data["hrvStatus"].lower()
    return [e]


def _dur(minutes):
    minutes = int(round(minutes))
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}"


def _num(v, digits=0):
    return f"{v:.{digits}f}" if isinstance(v, (int, float)) else str(v)


def night_infos(readings, stages, breathing, restless, extras, resp, tz, days=None):
    """Everything known about each night, keyed by its evening date."""
    nights = {}
    def get(n):
        return nights.setdefault(n, {"scores": {}, "extras": {}})
    for sess in sorted(stages, key=lambda x: (x["start"], "quality" not in (x.get("scores") or {}))):
        info = get(night_of(sess["start"], tz))
        if "stages" not in info:
            info["start"], info["stop"] = sess["start"], sess["stop"]
            mins = {"Deep": 0, "Light": 0, "REM": 0, "WAKE": 0}
            for t0, t1, st in sess["intervals"]:
                if st in mins:
                    mins[st] += (t1 - t0) / 60
            info["stages"] = mins
            info["awakenings"] = sess.get("awakenings")
        for k, v in (sess.get("scores") or {}).items():
            if not isinstance(info["scores"].get(k), (int, float)):
                info["scores"][k] = v
        for k, v in (sess.get("ranges") or {}).items():
            info.setdefault("ranges", {}).setdefault(k, v)
    for n, sess in _dedupe_by_night(breathing, tz).items():
        info = get(n)
        info["breathing"] = sess
        info.setdefault("start", sess["start"])
        info.setdefault("stop", sess["stop"])
    for n, sess in _dedupe_by_night(restless, tz).items():
        info = get(n)
        info["restless"] = sess["count"]
        info.setdefault("start", sess["start"])
        info.setdefault("stop", sess["stop"])
    by_night = {}
    for t, v, _ in readings:
        by_night.setdefault(night_of(t, tz), []).append((t, v))
    for n, rs in by_night.items():
        if len(rs) >= 30:
            vals = [v for _, v in rs]
            low = min(rs, key=lambda r: (r[1], r[0]))
            get(n)["spo2"] = {"avg": sum(vals) / len(vals), "min": low[1], "min_t": low[0],
                              "below90": sum(1 for v in vals if v < 90)}
    for e in extras:
        info = nights.get(night_of(e["t"], tz))
        if info is not None:
            for k, v in e.items():
                if k != "t":
                    info["extras"].setdefault(k, v)
    for n, info in nights.items():
        if "resp" not in info["extras"] and "start" in info and resp:
            vals = [r for t, r in resp.items() if info["start"] <= t <= info["stop"]]
            if vals:
                info["extras"]["resp"] = sum(vals) / len(vals)
    return nights


GOOD_COLOR, BAD_COLOR = "#2e7d32", "#c62828"
MICRO_GOOD_PER_H, MICRO_BAD_PER_H = 5, 10   # movement-based micro-awakenings per hour
MIN_NIGHT_MINUTES = 180     # less sleep than this: the Journal note only gives the day before
# Reference ranges when Garmin's own (personal) optimal ranges are not available
DEFAULT_RANGES = {"deep": (16, 33), "rem": (21, 31), "light": (30, 64), "total": (420, 540),
                  "awakenings": (0, 1), "stress": (0, 25)}
GOOD_WORDS, BAD_WORDS = {"excellent", "good"}, {"poor"}


def _esc(x):
    return str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _c(text, good):
    """Green if good is True, red if False, plain if None."""
    if good is None:
        return _esc(text)
    return f'<span style="color:{GOOD_COLOR if good else BAD_COLOR}">{_esc(text)}</span>'


def _judge(v, good_if, bad_if):
    """True / False / None from two predicates."""
    if v is None:
        return None
    if good_if(v):
        return True
    if bad_if(v):
        return False
    return None


def _score_txt(v):
    """Garmin scores are out of 100 (numbers from .fit); Garmin Connect gives a word."""
    return f"{v:.0f}/100" if isinstance(v, (int, float)) else str(v)


def _score_label(name, v):
    return f"{name} score " if isinstance(v, (int, float)) else f"{name} "


def _score_judge(v):
    if isinstance(v, (int, float)):
        return _judge(v, lambda x: x >= 80, lambda x: x < 60)
    w = str(v).lower()
    return True if w in GOOD_WORDS else False if w in BAD_WORDS else None


def journal_note(info, tz):
    """The note shown in OSCAR: one line per topic, title in bold, values in
    green when good and red when bad (Garmin's personal optimal ranges when
    known, otherwise usual reference values)."""
    sc, ex = info["scores"], info["extras"]
    rng = dict(DEFAULT_RANGES, **(info.get("ranges") or {}))
    lines = []

    def line(title, items):
        items = [i for i in items if i]
        if items:
            lines.append(f"<b>{_esc(title)}:</b> " + " · ".join(items))

    # Sleep
    items = []
    if "start" in info:
        items.append(_esc(f"{to_local(info['start'], tz):%H:%M}-{to_local(info['stop'], tz):%H:%M}"))
    m = info.get("stages")
    asleep = (m["Deep"] + m["Light"] + m["REM"]) if m else 0
    if m and asleep:
        lo, hi = rng["total"]
        if lo == hi:            # Garmin gives your sleep need: reaching it is good
            good = asleep >= lo
        else:
            good = True if lo <= asleep <= hi else False if asleep < lo else None
        items.append("total " + _c(_dur(asleep), good))
    if "overall" in sc:
        items.append("score " + _c(_score_txt(sc["overall"]), _score_judge(sc["overall"])))
    if "quality" in sc:
        items.append("quality " + _c(_score_txt(sc["quality"]), _score_judge(sc["quality"])))
    line("Sleep", items)

    # Stages
    if m:
        def stage(name, key, label):
            mins = m[name]
            pct = 100 * mins / asleep if asleep else 0
            lo, hi = rng[key]
            if key == "light":
                good = True if lo <= pct <= hi else None
            else:
                good = pct >= lo
            return label + " " + _c(f"{_dur(mins)} ({pct:.0f} %)", good)
        items = [stage("Deep", "deep", "deep"), stage("Light", "light", "light"), stage("REM", "rem", "REM"),
                 "awake " + _c(_dur(m["WAKE"]), _judge(m["WAKE"], lambda x: x <= 30, lambda x: x > 60))]
        if info.get("awakenings") is not None:
            hi = rng["awakenings"][1]
            items.append(_c(f"{info['awakenings']} awakenings",
                            _judge(info["awakenings"], lambda x: x <= hi, lambda x: x >= hi + 3)))
        line("Stages", items)

    # SpO2
    if "spo2" in info:
        o = info["spo2"]
        line("SpO2", [
            "avg " + _c(f"{o['avg']:.1f} %", _judge(o["avg"], lambda x: x >= 95, lambda x: x < 93)),
            "min " + _c(f"{o['min']} %", _judge(o["min"], lambda x: x >= 90, lambda x: x < 88))
            + _esc(f" at {to_local(o['min_t'], tz):%H:%M}"),
            _c(f"{o['below90']} min under 90 %", _judge(o["below90"], lambda x: x == 0, lambda x: x > 10)),
        ])

    # Breathing
    items = []
    if "resp" in ex:
        items.append("rate " + _c(f"{ex['resp']:.1f}/min",
                                  _judge(ex["resp"], lambda x: 12 <= x <= 20, lambda x: x < 10 or x > 22)))
    if "breathing" in info:
        b = info["breathing"]
        mins = {"low": 0, "medium": 0, "high": 0}
        for t0, t1, sev in b["periods"]:
            if sev in mins:
                mins[sev] += (t1 - t0) / 60
        parts = [_c(f"{k} {_dur(v)}", None if k == "low" else False) for k, v in mins.items() if v >= 1]
        items.append("disruptions " + (", ".join(parts) if parts else _c("none", True)))
        if b.get("overall"):
            items.append("overall " + _c(b["overall"], True if b["overall"] == "none"
                                         else False if b["overall"] in ("medium", "high") else None))
    line("Breathing", items)

    # Night recording (activity app: 1 s SpO2 / pulse, 100 Hz movement)
    r = info.get("night_rec")
    if r:
        hours = max((r["stop"] - r["start"]) / 3600, 0.1)
        rate = r["micro"] / hours
        items = [_esc(f"{to_local(r['start'], tz):%H:%M}-{to_local(r['stop'], tz):%H:%M}"),
                 _c(f"{r['micro']} micro-awakenings ({rate:.1f}/h)",
                    _judge(rate, lambda x: x < MICRO_GOOD_PER_H, lambda x: x > MICRO_BAD_PER_H))]
        aw = r["awakenings"]
        if aw:
            good = (True if len(aw) <= 2 and r["awake_min"] <= 20
                    else False if len(aw) >= 5 or r["awake_min"] > 45 else None)
            items.append(_c(f"{len(aw)} awakenings ({_dur(r['awake_min'])})", good)
                         + _esc(" at " + ", ".join(f"{to_local(a, tz):%H:%M}" for a, _ in aw)))
        else:
            items.append(_c("no awakening", True))
        if r.get("hr"):
            items.append(_esc(f"pulse avg {r['hr'][0]:.0f} (min {r['hr'][1]}, max {r['hr'][2]})"))
        line("Night recording", items)
        ps = r.get("position")
        if ps:
            total = sum(ps["mins"].values()) or 1
            items = [_esc(f"{p} {_dur(ps['mins'][p])} ({100 * ps['mins'][p] / total:.0f} %)")
                     for p in ("back", "right", "left", "belly", "upright")]
            items.append(_esc(f"{ps['changes']} changes"))
            line("Position", items)

    # Heart and recovery
    items = []
    if "hrv" in ex:
        st = ex.get("hrv_status")
        good = True if st == "balanced" else False if st in ("low", "poor") else None
        details = [x for x in (st, f"weekly {ex['hrv_weekly']:.0f}" if "hrv_weekly" in ex else None) if x]
        items.append("HRV " + _c(f"{ex['hrv']:.0f} ms" + (f" ({', '.join(details)})" if details else ""), good))
    if "rhr" in ex:
        items.append(_esc(f"resting HR {ex['rhr']:.0f}"))
    if "avg_hr" in ex:
        items.append(_esc(f"avg HR {ex['avg_hr']:.0f}"))
    if "skin_temp" in ex:
        items.append("skin temp " + _c(f"{ex['skin_temp']:+.1f} °C",
                                       _judge(abs(ex["skin_temp"]), lambda x: x <= 0.5, lambda x: x >= 1.0)))
    stress = ex.get("stress", sc.get("stress"))
    if stress is not None:
        hi = rng["stress"][1]
        items.append("sleep stress " + _c(_num(stress), _judge(stress, lambda x: x <= hi, lambda x: x > 50)))
    if "body_battery" in ex:
        items.append("Body Battery " + _c(f"{ex['body_battery']:+.0f}",
                                          _judge(ex["body_battery"], lambda x: x >= 50, lambda x: x < 20)))
    line("Heart and recovery", items)

    # Restlessness
    items = []
    rating = _score_judge(sc["restlessness_rating"]) if "restlessness_rating" in sc else None
    if "restless" in info:
        items.append(_c(f"{info['restless']} restless moments", rating))
    if "restlessness" in sc:
        items.append(_score_label("restlessness", sc["restlessness"]) + _c(_score_txt(sc["restlessness"]),
                                                rating if rating is not None else _score_judge(sc["restlessness"])))
    if "interruptions" in sc:
        items.append(_score_label("interruptions", sc["interruptions"])
                     + _c(_score_txt(sc["interruptions"]), _score_judge(sc["interruptions"])))
    line("Restlessness", items)

    # Day before
    d = info.get("day_before")
    if d:
        items = []
        if "steps" in d:
            items.append(_c(f"{d['steps']:,} steps", _judge(d["steps"], lambda x: x >= 7500, lambda x: x < 3000)))
        if d.get("distance_km"):
            items.append(_esc(f"{d['distance_km']:.1f} km"))
        if d.get("active_min"):
            items.append(_esc(f"{_dur(d['active_min'])} active"))
        if d.get("active_kcal"):
            items.append(_esc(f"{d['active_kcal']:.0f} active kcal"))
        if "moderate_min" in d:
            total = d["moderate_min"] + 2 * d["vigorous_min"]
            items.append(_c(f"{total:.0f} intensity min ({d['moderate_min']:.0f} moderate, "
                            f"{d['vigorous_min']:.0f} vigorous)",
                            _judge(total, lambda x: x >= 22, lambda x: x == 0)))
        if d.get("floors"):
            items.append(_esc(f"{d['floors']:.0f} floors"))
        if "stress" in d:
            items.append("stress " + _c(f"{d['stress']:.0f}", _judge(d["stress"], lambda x: x <= 25, lambda x: x > 50)))
        if "bb_charged" in d or "bb_drained" in d:
            items.append(_esc(f"Body Battery +{d.get('bb_charged', 0):.0f}/-{d.get('bb_drained', 0):.0f}"))
        if d.get("activities"):
            items.append(_esc("activities: " + "; ".join(
                f"{n} {_dur(mm)}" + (f" {k:.1f} km" if k >= 0.05 else "") for n, mm, k in d["activities"])))
        line("Day before", items)

    if not lines:
        return None
    return "<p><b>Garmin</b><br/>" + "<br/>".join(lines) + "</p>"


def write_journal_xml(path, readings, stages, breathing, restless, extras, resp, tz,
                      overwrite=False, log=print, days=None, night_recs=None):
    """OSCAR Journal backup file (OSCAR: restore/import Journal): one note and
    per night. Cumulative like the other files. OSCAR skips a note
    it already has, and puts a different one above the existing text."""
    import xml.etree.ElementTree as ET
    infos = night_infos(readings, stages, breathing, restless, extras, resp, tz)
    today = dt.date.today()
    for n, r in _dedupe_by_night(night_recs or [], tz).items():
        infos.setdefault(n, {"scores": {}, "extras": {}})["night_rec"] = r["summary"]
    for day, d in (days or {}).items():
        n = dt.date.fromisoformat(day)
        if n < today:      # also nights without sleep data (e.g. an activity was running)
            infos.setdefault(n, {"scores": {}, "extras": {}})["day_before"] = d
    new_days = {}
    for n, info in infos.items():
        m = info.get("stages")
        rec = "night_rec" in info
        if m and (m["Deep"] + m["Light"] + m["REM"]) < MIN_NIGHT_MINUTES:
            # Only a scrap of sleep (e.g. an activity started during the night): the
            # night's figures would be meaningless, keep the day before only, plus
            # the night app's recording if there is one.
            if "day_before" not in info and not rec:
                continue
            info = {"scores": {}, "extras": {},
                    **{k: info[k] for k in ("day_before", "night_rec") if k in info},
                    **({"spo2": info["spo2"]} if rec and "spo2" in info else {})}
        elif "stages" not in info and "breathing" not in info and not rec:
            # No sleep data: either the night is not finished yet (some SpO2 already, its
            # note would change later) or the watch recorded no sleep at all (e.g. an
            # activity was running): only the latter gets a note, with the day before.
            if "spo2" in info or "day_before" not in info:
                continue
        note = journal_note(info, tz)
        if not note:
            continue
        day = ET.Element("day", {"date": str(n), "lastupdated": ""})
        ET.SubElement(ET.SubElement(day, "note"), "text").text = note
        new_days[str(n)] = day
    if not new_days:
        return None
    old_days = {}
    if not overwrite and os.path.exists(path):
        try:
            old = ET.parse(path).getroot()
            old_days = {d.get("date"): d for d in old.iter("day")}
        except ET.ParseError:
            os.replace(path, path + ".old")
            log(f"   (unreadable file: the previous one was kept as {path}.old)")
    added = len(set(new_days) - set(old_days))
    updated = len(set(new_days) & set(old_days))
    merged = {**old_days, **new_days}
    root = ET.Element("OSCAR", {"created": dt.datetime.now().replace(microsecond=0).isoformat()})
    journal = ET.SubElement(root, "Journal", {"username": "Garmin import"})
    for n in sorted(merged):
        journal.append(merged[n])
    ET.indent(root, space="  ")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(ET.tostring(root, encoding="utf-8"))
        f.write(b"\n")
    log(f"-> {path}: {added} night(s) added, {updated} updated, {len(merged)} in total")
    return path


# ---------------------------------------------------------------------------
# CPAP clock correction (optional): ResMed AirSense 11 read over Bluetooth
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.expanduser("~/.garmin2oscar.json")


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=1)
    except OSError as e:
        print(f"(could not save {CONFIG_PATH}: {e})")


def add_clock_options(ap):
    ap.add_argument("--airsense", nargs="?", const="", default=None, metavar="DEVICE",
                    help="read the ResMed AirSense 11 clock over Bluetooth (airbreak-plus) and shift "
                         "every Garmin time by its offset, so both line up in OSCAR. DEVICE (alias or "
                         "UUID) is only needed the first time: it is saved in ~/.garmin2oscar.json")
    ap.add_argument("--airbreak", default=None, metavar="PATH",
                    help="airbreak-plus folder (default: the saved one, else ~/airbreak-plus)")
    ap.add_argument("--chest", action="store_true",
                    help="the watch is taped on the chest during the night app recording: the body "
                         "position (back / left / right / belly) goes to OSCAR's Inclination graph "
                         "(Somnopose file, written even without --somnopose), the Journal and the summary")
    ap.add_argument("--chest-flip", action="store_true",
                    help="with --chest: the watch is upside down (swaps left and right)")
    ap.add_argument("--clock-offset", type=float, default=None, metavar="SECONDS",
                    help="shift every Garmin time by this many seconds (CPAP clock ahead > 0), "
                         "without Bluetooth")


def setup_clock(a, log=print):
    """Applies --clock-offset or measures the AirSense clock (--airsense)."""
    if a.clock_offset is not None:
        set_clock_offset(a.clock_offset)
        log(f"Clock correction: {CLOCK_OFFSET:+d} s (--clock-offset).")
        return
    if a.airsense is None:
        return
    cfg = load_config()
    here = os.path.dirname(os.path.abspath(__file__))
    # airbreak-plus: given, saved, next to this script (setup.sh puts it there), or in ~
    airbreak = a.airbreak or cfg.get("airbreak") or next(
        (p for p in (os.path.join(here, "airbreak-plus"), os.path.expanduser("~/airbreak-plus"))
         if os.path.exists(os.path.join(p, "python", "lib", "as11_ble.py"))),
        os.path.join(here, "airbreak-plus"))
    # the machine: given, saved, or the only one paired with airbreak-plus (~/.as11_ble.json)
    device = a.airsense or cfg.get("airsense_device")
    if not device:
        try:
            with open(os.path.expanduser("~/.as11_ble.json")) as f:
                paired = json.load(f)
        except (OSError, ValueError):
            paired = {}
        if len(paired) == 1:
            addr, info = next(iter(paired.items()))
            device = (info or {}).get("alias") or addr
            log(f"AirSense: using the machine paired with airbreak-plus ({device}).")
    if not device:
        sys.exit("--airsense: no paired AirSense found. Pair it once with airbreak-plus "
                 "(see README), or give its alias / UUID: --airsense as11")
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import airsense_clock_check as acc
        m = acc.measure_offset(airbreak, device, log=log)
    except Exception as e:
        last = cfg.get("last_offset")
        if last:
            set_clock_offset(last["seconds"])
            log(f"AirSense not reachable ({e}). Using the last measured offset "
                f"{CLOCK_OFFSET:+d} s (from {last['measured']}).")
        else:
            log(f"AirSense not reachable ({e}): no clock correction.")
        return
    set_clock_offset(m["offset"])
    cfg.update({"airsense_device": device, "airbreak": airbreak,
                "last_offset": {"seconds": m["offset"], "precision": m["half_width"],
                                "measured": dt.datetime.now().replace(microsecond=0).isoformat()}})
    save_config(cfg)
    direction = "ahead of" if m["offset"] > 0 else "behind" if m["offset"] < 0 else "in step with"
    log(f"AirSense clock {direction} this computer by {abs(m['offset']):.1f} s "
        f"(± {m['half_width']:.1f} s): every Garmin time is shifted by {CLOCK_OFFSET:+d} s.")


def make_tz(name=None, offset_hours=None):
    if offset_hours is not None:
        return int(offset_hours * 3600)
    if name:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    return None


def main():
    ap = argparse.ArgumentParser(description="Garmin .fit (SpO2) -> .dat for OSCAR")
    ap.add_argument("fit", nargs="+")
    ap.add_argument("-o", "--outdir", default=".")
    ap.add_argument("--min-confidence", type=int, default=0)
    ap.add_argument("--min-minutes", type=int, default=30)
    ap.add_argument("--max-hold", type=int, default=120)
    ap.add_argument("--split-gap", type=int, default=10)
    ap.add_argument("--timezone", default=None)
    ap.add_argument("--tz-offset", type=float, default=None)
    ap.add_argument("--no-csv", action="store_true", help="do not write the .csv file")
    ap.add_argument("--format", choices=["viatom", "md300"], default="viatom",
                    help="viatom (default): import all nights at once via "
                         "'Import Viatom/Wellue data'; md300: .dat for the Oximetry Wizard")
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
                    help="don't write the OSCAR Journal file (a note per night)")
    ap.add_argument("--journal", dest="journal", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--overwrite", action="store_true",
                    help="start the cumulative files (summary, stages, breathing, Somnopose) "
                         "from scratch instead of adding to them")
    add_clock_options(ap)
    a = ap.parse_args()
    setup_clock(a)

    readings, hr, _ = extract_many(a.fit)
    tz = make_tz(a.timezone, a.tz_offset)
    sessions, breathing, extras, resp, daily = [], [], [], {}, {}
    for p in a.fit:
        try:
            sessions += extract_sleep(p)
            breathing += extract_disruptions(p)
            extras += extract_extras(p)
            resp.update(extract_respiration(p))
            merge_daily(daily, extract_daily_activity(p, tz))
        except Exception:
            pass
    days = daily_totals_from_fit(daily)
    night_recs, rec_readings = [], []
    for p in a.fit:
        try:
            rec = extract_night_recording(p)
        except Exception:
            rec = None
        if rec:
            nr, mr = handle_night_recording(rec, tz, a.outdir, not a.no_csv, watch_readings=readings,
                                            watch_hr=hr, chest=a.chest, chest_flip=a.chest_flip)
            night_recs.append(nr)
            rec_readings += mr
    if night_recs:
        # the night's watch readings are in the night recording's file (one file per night)
        rec_nights = {night_of(nr["start"], tz) for nr in night_recs}
        outside = [r for r in readings if night_of(r[0], tz) in rec_nights
                   and not any(nr["start"] <= r[0] <= nr["stop"] for nr in night_recs)]
        readings = [r for r in readings if night_of(r[0], tz) not in rec_nights]
        rec_readings = outside + rec_readings
    if not readings and not sessions and not breathing and not night_recs:
        sys.exit("No SpO2 readings or sleep stages found (is SpO2 tracking enabled on the watch?)")
    if readings:
        created = convert(readings, hr, tz, a.outdir, a.min_confidence, a.min_minutes,
                          a.max_hold, a.split_gap, not a.no_csv, fmt=a.format)
        if not created:
            print("No SpO2 segment long enough (see --min-minutes).")
    if sessions and not a.no_stages:
        write_dreem_csv(os.path.join(a.outdir, "oscar_dreem", "garmin_sleep_stages.csv"), sessions, tz,
                        overwrite=a.overwrite)
        print("Sleep stages: in OSCAR, Import Dreem data and pick oscar_dreem/garmin_sleep_stages.csv")
    if breathing and not a.no_breathing:
        write_breathing_csv(a.outdir, breathing, tz, overwrite=a.overwrite)
    readings = readings + rec_readings          # for the Journal and the summary
    # --chest alone also writes the Somnopose file, with the position nights only
    somno_br, somno_nr = ((breathing, night_recs) if a.somnopose
                          else ([], [n for n in night_recs if n.get("position")]) if a.chest else ([], []))
    if somno_br or somno_nr:
        if write_somnopose_csv(os.path.join(a.outdir, "oscar_somnopose", "garmin_somnopose.csv"),
                               somno_br, [], tz, overwrite=a.overwrite, night_recs=somno_nr):
            print("In OSCAR, Import Somnopose data and pick oscar_somnopose/garmin_somnopose.csv")
    if a.journal:
        if write_journal_xml(os.path.join(a.outdir, "oscar_journal", "garmin_journal.xml"), readings,
                             sessions, breathing, [], extras, resp, tz, overwrite=a.overwrite,
                             days=days, night_recs=night_recs):
            print("In OSCAR: File > Backup Journal first. Then display a day that is NOT in this file\n"
                  "  (e.g. before your first Garmin night) whose Notes box is NOT empty (type a\n"
                  "  temporary 'x' if needed, remove it afterwards), then File > Import Journal.\n"
                  "  OSCAR's import relies on the note box on screen: if it is empty, the notes\n"
                  "  of the imported days are erased; if the day on screen is in the file, its\n"
                  "  note is replaced.")
    if not a.no_summary:
        write_summary_csv(os.path.join(a.outdir, "garmin_nights_summary.csv"),
                          readings, sessions, breathing, [], tz, overwrite=a.overwrite, days=days,
                          night_recs=night_recs)


if __name__ == "__main__":
    main()
