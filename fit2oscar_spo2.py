#!/usr/bin/env python3
"""
fit2oscar_spo2.py - Converts SpO2 (and pulse) from Garmin .fit files
(WELLNESS / monitoring) into files OSCAR can import.

Default output: one Wellue/Viatom O2Ring file per night, in ./oscar_viatom/.
In OSCAR: Tools > Import Viatom/Wellue data, select every file in that folder
(Cmd+A): all nights are imported at once, nights already imported are skipped.
With --format md300: one ChoiceMMed MD300 .dat per night, to import one at a
time via Oximetry Wizard > Import from a data file.

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

md300 .dat format (read by MD300W1Loader::readDATFile in OSCAR):
    byte 0: id, bytes 1-2: number of records (little-endian)
    then n records of 11 bytes:
        0 0 id YY MM DD hh mm ss SpO2 Pulse   (local time, 1 record per second)
    A value of 0 means no data (OSCAR breaks the trace there).
"""

import argparse
import datetime as dt
import os
import struct
import sys

FIT_EPOCH = dt.datetime(1989, 12, 31, tzinfo=dt.timezone.utc)


def to_local(t, tz=None):
    """FIT timestamp (UTC) -> local time, daylight saving time included.

    tz: None = the computer's time zone, int = fixed offset in seconds,
        or a tzinfo object (e.g. zoneinfo.ZoneInfo("Europe/Paris")).
    """
    utc = FIT_EPOCH + dt.timedelta(seconds=t)
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
            if v != fmt[2]:
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


VIATOM_STEP = 4          # seconds per record, as in Wellue/Viatom O2Ring files
VIATOM_HEADER = 40


def write_viatom(path, seg, hr_times, hr, tz, max_hold):
    """Writes a Wellue/Viatom O2Ring file (format 5), read by OSCAR's ViatomLoader.

    Header (40 bytes): u16 version=5, u16 year, month, day, hour, min, sec,
    u24 file size, 0, u16 duration (s), 0, 0, spo2 avg, spo2 min, ...,
    u16 time < 90 % (s), ..., avg pulse, zeros.
    Records (5 bytes, one per 4 s): spo2, pulse, invalid flag, motion, vibration.
    An invalid record is FF FF FF 00 00. OSCAR takes the start time from the
    14-digit local timestamp in the file name.
    """
    start, spo2_s = per_second_spo2(seg, max_hold)
    n = min(len(spo2_s) // VIATOM_STEP, 65535 // VIATOM_STEP)
    recs = bytearray()
    good_o2, good_hr = [], []
    for k in range(n):
        t = start + k * VIATOM_STEP
        o2 = spo2_s[k * VIATOM_STEP]
        pr = nearest_hr(hr_times, hr, t, tol=300) if o2 else 0
        if o2 and 30 <= pr <= 250:
            if o2 < 61:          # OSCAR's Viatom import ignores values below 61 %
                o2 = 0xFF
            else:
                good_o2.append(o2)
            good_hr.append(pr)
            recs += bytes([o2, pr, 0, 0, 0])
        else:                    # no SpO2, or no pulse nearby: gap in the trace
            recs += bytes([0xFF, 0xFF, 0xFF, 0, 0])
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
            fmt="viatom"):
    """Converts readings into OSCAR oximetry files (plus a .csv if csv=True).

    readings: list of (fit_timestamp_utc, spo2, confidence)
    hr:       dict {fit_timestamp_utc: bpm}
    tz:       time zone (see to_local); None = the computer's
    window:   (start, end) as FIT UTC timestamps; only segments starting
              inside this window are written.
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
    for seg in build_segments(readings, split_gap * 60):
        if (seg[-1][0] - seg[0][0]) / 60 < min_minutes:
            continue
        if window and not (window[0] <= seg[0][0] < window[1]):
            continue
        lt0 = to_local(seg[0][0], tz)
        base = os.path.join(outdir, "garmin_spo2_" + lt0.strftime("%Y%m%d%H%M%S"))
        if fmt == "md300":
            out = base + ".dat"
            write_dat(out, seg, hr_times, hr, tz, max_hold)
        else:
            os.makedirs(vdir, exist_ok=True)
            out = os.path.join(vdir, "Garmin_" + lt0.strftime("%Y%m%d%H%M%S"))
            write_viatom(out, seg, hr_times, hr, tz, max_hold)
        vals = [r[1] for r in seg]
        lt1 = to_local(seg[-1][0], tz)
        log(f"-> {out}: {lt0:%Y-%m-%d %H:%M} to {lt1:%Y-%m-%d %H:%M} (local time), "
            f"{len(seg)} readings, SpO2 avg {sum(vals)/len(vals):.1f}% min {min(vals)}%")
        if csv:
            with open(base + ".csv", "w") as f:
                f.write("Time,SpO2,Pulse,Confidence\n")
                for t, v, c in seg:
                    lt = to_local(t, tz)
                    f.write(f"{lt:%Y-%m-%d %H:%M:%S},{v},{nearest_hr(hr_times, hr, t)},{c}\n")
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
    a = ap.parse_args()

    readings, hr, _ = extract_many(a.fit)
    tz = make_tz(a.timezone, a.tz_offset)
    if not readings:
        sys.exit("No SpO2 readings found (is SpO2 tracking enabled on the watch?)")
    created = convert(readings, hr, tz, a.outdir, a.min_confidence, a.min_minutes,
                      a.max_hold, a.split_gap, not a.no_csv, fmt=a.format)
    if not created:
        print("No segment long enough (see --min-minutes).")


if __name__ == "__main__":
    main()
