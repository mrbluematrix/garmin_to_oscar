#!/usr/bin/env python3
"""
airsense_clock_check.py — lit l'heure d'une ResMed AirSense 11 en Bluetooth
et la compare à l'horloge du Mac. LECTURE SEULE : n'envoie que GetDateTime.

Repose sur la bibliothèque BLE d'airbreak-plus (github.com/m-kozlowski/airbreak-plus).

Usage :
    python3 airsense_clock_check.py --airbreak ~/airbreak-plus -d as11
    python3 airsense_clock_check.py --airbreak ~/airbreak-plus -d as11 --samples 20

  -d accepte l'alias ou l'UUID CoreBluetooth donné lors de l'appairage
  (as11_config.py devices pair ...).

Utilisable aussi comme module : measure_offset() renvoie le décalage, et
garmin2oscar_spo2.py --airsense s'en sert pour recaler les heures Garmin sur
l'horloge de l'AirSense.
"""

from __future__ import annotations

import argparse
import datetime as dt
import statistics
import sys
import time
from pathlib import Path


def load_airbreak(path: Path):
    py = Path(path).expanduser().resolve() / "python"
    if not (py / "lib" / "as11_ble.py").exists():
        raise FileNotFoundError(f"airbreak-plus introuvable dans {path} (attendu : python/lib/as11_ble.py)")
    sys.path.insert(0, str(py))
    sys.path.insert(0, str(py / "lib"))
    from as11_ble import BleTransport  # noqa: E402
    return BleTransport


def parse_device_time(resp: dict) -> float:
    """Extrait dateTime (ISO 8601 UTC) de la réponse et renvoie un timestamp epoch."""
    if "error" in resp:
        raise RuntimeError(f"erreur RPC : {resp['error']}")
    result = resp.get("result", resp)
    stamp = result["dateTime"]
    return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def fmt_offset(seconds: float) -> str:
    sign = "+" if seconds >= 0 else "-"
    s = abs(seconds)
    m, s = divmod(s, 60)
    return f"{sign}{int(m)} min {s:06.3f} s" if m else f"{sign}{s:.3f} s"


def measure_offset(airbreak, device, samples=12, interval=0.27, timeout=5.0, debug=False, log=print):
    """Connects to the AirSense (read only: GetDateTime) and returns
    {"offset": s, "half_width": s, "rtt": s, "samples": n}.
    offset > 0: the AirSense clock is ahead of this computer's clock."""
    BleTransport = load_airbreak(airbreak)
    log(f"Connexion à {device}…")
    t = BleTransport(address=device, debug=debug)
    t.connect()
    readings = []  # (t0, t1, device_epoch)
    try:
        for i in range(samples):
            t0 = time.time()
            resp = t.rpc("GetDateTime", None, timeout=timeout)
            t1 = time.time()
            readings.append((t0, t1, parse_device_time(resp)))
            if i + 1 < samples:
                time.sleep(interval)
    finally:
        t.close()

    # La machine renvoie une heure arrondie à la seconde (RTC à résolution 1 s).
    # Pour chaque lecture, l'heure réelle de la machine se situe dans [D, D+1)
    # à un instant compris entre t0 et t1, donc :
    #     décalage ∈ [D - t1, D + 1 - t0)
    # En croisant plusieurs lectures (espacées d'une fraction de seconde
    # non multiple de 1), l'intervalle se resserre bien en dessous de la seconde.
    whole_seconds = all(abs(d - round(d)) < 1e-6 for _, _, d in readings)
    rtts = [t1 - t0 for t0, t1, _ in readings]
    if whole_seconds:
        lo = max(d - t1 for t0, t1, d in readings)
        hi = min(d + 1 - t0 for t0, t1, d in readings)
        if lo <= hi:
            offset, half_width = (lo + hi) / 2, (hi - lo) / 2
        else:  # incohérence (latence BLE irrégulière) : estimation simple
            offsets = [d + 0.5 - (t0 + t1) / 2 for t0, t1, d in readings]
            offset, half_width = statistics.median(offsets), 0.5
    else:
        offsets = [d - (t0 + t1) / 2 for t0, t1, d in readings]
        offset = statistics.median(offsets)
        half_width = max(rtts) / 2
    return {"offset": offset, "half_width": half_width, "rtt": statistics.median(rtts),
            "samples": len(readings)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare l'horloge de l'AirSense 11 à celle du Mac")
    ap.add_argument("--airbreak", default="~/airbreak-plus", type=Path,
                    help="dossier du dépôt airbreak-plus (défaut : ~/airbreak-plus)")
    ap.add_argument("-d", "--device", required=True,
                    help="alias ou UUID BLE de la machine (déjà appairée avec as11_config.py)")
    ap.add_argument("--samples", type=int, default=12,
                    help="nombre de lectures (défaut : 12)")
    ap.add_argument("--interval", type=float, default=0.27,
                    help="pause entre lectures en secondes (défaut : 0.27)")
    ap.add_argument("--timeout", type=float, default=5.0)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    try:
        m = measure_offset(args.airbreak, args.device, args.samples, args.interval, args.timeout, args.debug)
    except FileNotFoundError as e:
        sys.exit(str(e))
    offset, half_width = m["offset"], m["half_width"]

    now = time.time()
    mac_local = dt.datetime.fromtimestamp(now).astimezone()
    dev_local = dt.datetime.fromtimestamp(now + offset).astimezone()

    print()
    print(f"Lectures         : {m['samples']}  (latence BLE médiane {m['rtt']*1000:.0f} ms)")
    print(f"Heure du Mac     : {mac_local:%Y-%m-%d %H:%M:%S.%f}"[:-3] + f" {mac_local:%Z}")
    print(f"Heure AirSense   : {dev_local:%Y-%m-%d %H:%M:%S.%f}"[:-3] + "  (convertie dans le fuseau du Mac)")
    print(f"Décalage         : {fmt_offset(offset)}  (± {half_width*1000:.0f} ms)")
    if offset > 0:
        print("                   → l'AirSense est EN AVANCE sur le Mac")
    elif offset < 0:
        print("                   → l'AirSense est EN RETARD sur le Mac")
    print()
    print("Astuce : vérifiez que le Mac est bien synchronisé (Réglages > Général > "
          "Date et heure > « Régler automatiquement »).")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
