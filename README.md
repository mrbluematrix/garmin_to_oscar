# UPDATE:
for non geek: easy online version (you have to manualy export your garmin health fit data from: 
https://connect.garmin.com/app/settings/accountInformation
(at bottom)

then upload to:
https://mrbluematrix.github.io/garmin_to_oscar/

then unzip & import to OSCAR
done.

# garmin2oscar
<img width="1776" height="1088" alt="Capture d’écran 2026-10-27 à 12 23 31" src="https://github.com/user-attachments/assets/8b202fea-2efe-4633-9a9b-4627cb522015" />

<img width="802" height="364" alt="journal" src="https://github.com/user-attachments/assets/43b01209-f9ea-49be-bdfe-d5fc3a1d0025" />


# garmin2oscar

Import the overnight **SpO2, pulses, motions, sleep stages, breathing disruptions & more** recorded by a Garmin watch into
[OSCAR](https://www.sleepfiles.com/OSCAR/), so you can see them next to your CPAP data.

OSCAR can't read Garmin `.fit` files. These scripts convert them into a format
OSCAR already supports (Wellue/Viatom O2Ring files, or ChoiceMMed MD300 `.dat`).

## Features

- Reads SpO2 readings (1 per minute during sleep) and pulse from Garmin wellness `.fit` files.
- Writes **one file per night**, imported all at once with OSCAR's
  **Import Viatom/Wellue data** (already-imported nights are skipped).
- Also writes a readable `.csv` per night (`Time,SpO2,Pulse,Confidence`).
- Exports Garmin's **breathing disruptions** ("breathing variations") as two `.csv` files:
  a summary per night and every period. OSCAR can't display them; open them in a
  spreadsheet to compare their trend with your CPAP's AHI.
- Writes `garmin_nights_summary.csv`, **one row per night** with what OSCAR can't show:
  Garmin's sleep, restlessness and interruptions scores, awakenings, restless moments,
  stage minutes, SpO2 summary and breathing disruptions. It also gives the **activity of the
  day before** each night (steps, distance, active time, active kcal; with
  `garmin2oscar_spo2.py` also intensity minutes, floors, stress, Body Battery and the
  day's activities), which also appears in the Journal note. Scores are numbers when they
  come from `.fit` files, Garmin Connect's ratings (e.g. "Fair") otherwise.
- Converts the **sleep stages** (deep, light, REM, awake) into one file for OSCAR's
  **Import Dreem data**, so OSCAR shows a sleep-stage graph next to your CPAP data.
- Handles daylight saving time. On a night that starts on a DST-change day,
  readings before midnight are dropped automatically, otherwise OSCAR labels
  that night's time axis 1 hour off (a display quirk in OSCAR).
- Optional automatic download from Garmin Connect, so no manual export is needed.
- **Restless moments** (Garmin Connect only, so with `garmin2oscar_spo2.py` or the
  `sleep.json` files on the web page) can be shown in OSCAR with `--somnopose`.
- With the automatic download, Garmin's per-minute **sleep movement** is added too
  (shown as OSCAR's Movement graph, value = Garmin activity level × 10). It isn't in
  the `.fit` files, so it's not available when converting an export.

## The two scripts

| Script | What it does | Requirements |
|---|---|---|
| `fit2oscar_spo2.py` | Converts `.fit` files you already have (Garmin Connect data export, or copied from the watch over USB) | Python 3.9+, nothing else |
| `garmin2oscar_spo2.py` | Logs in to Garmin Connect, downloads the files and converts them | Python 3.12+ and [`garminconnect`](https://github.com/cyberjunky/python-garminconnect); needs `fit2oscar_spo2.py` in the same folder |

## Installation

```bash
git clone https://github.com/YOUR-USERNAME/garmin2oscar.git
cd garmin2oscar
python3 -m venv .venv
.venv/bin/pip install garminconnect      # Windows: .venv\Scripts\pip install garminconnect
```

If you get `externally-managed-environment` (Homebrew Python), that's why the
commands use a virtual environment. Don't use `--break-system-packages`.

## Usage

### Download from Garmin Connect

```bash
.venv/bin/python garmin2oscar_spo2.py                     # last night
.venv/bin/python garmin2oscar_spo2.py --nights 7          # last 7 nights
.venv/bin/python garmin2oscar_spo2.py --night 2026-10-18  # night starting that evening
```

The first run asks for your Garmin e-mail, password and 2FA code if you have one.
After that, session tokens are stored in `~/.garminconnect`; your password is never saved.
Downloaded `.fit` files are cached in `./garmin_fit/`.

### Convert `.fit` files you already have

```bash
python3 fit2oscar_spo2.py *_WELLNESS.fit
```

### Import into OSCAR

1. Choose **Import Viatom/Wellue data**.
2. Open the `oscar_viatom` folder created by the script.
3. Select all files (Cmd+A / Ctrl+A) and confirm.
4. For sleep stages, choose **Import Dreem data** and pick
   `oscar_dreem/garmin_sleep_stages.csv`. Nights already imported are skipped.
5. With `--somnopose` (or the matching box on the web page), choose
   **Import Somnopose data** and pick `oscar_somnopose/garmin_somnopose.csv`.
6. For the Journal (notes and bookmarks), **follow these steps exactly**, because OSCAR's
   Journal import relies on the note box shown on screen (bug in OSCAR):
   1. **File → Backup Journal** (safety copy).
   2. Display a day that is **not** in `garmin_journal.xml` (e.g. before your first night
      with the watch) and whose **Notes box is not empty**: type a temporary "x" if needed.
   3. **File → Import Journal** and pick `oscar_journal/garmin_journal.xml`.
   4. Remove the temporary "x", change day, and check a couple of days.

   If the note box is empty during the import, OSCAR **erases** the notes of the imported
   days; if the day on screen is in the file, its note is **replaced** by the Garmin one.

Prefer the Oximetry Wizard? Add `--format md300` to get ChoiceMMed `.dat` files.
Import them one night at a time with *Import from a data file* and choose
*use the oximeter's internal clock*. If you abandon a wizard import halfway,
restart OSCAR before the next one.

### Running it every day

The files that gather several nights (`garmin_nights_summary.csv`, the sleep stages,
the breathing files and the Somnopose file) are **cumulative**: each run adds its
nights and updates the ones it covers again, so a daily run builds up a full history.
Re-importing them into OSCAR is safe: it skips the nights it already has.

## Options

| Option | Effect |
|---|---|
| `--format viatom\|md300` | Output format (default `viatom`) |
| `--no-csv` | Don't write the `.csv` files |
| `--no-stages` | Don't write the sleep-stage file |
| `--no-breathing` | Don't write the breathing-disruption files |
| `--somnopose` | Also write a file for OSCAR's **Import Somnopose data**: breathing disruptions as an "Orientation" graph (0 not measured, 10 none, 20 low, 30 medium, 40 high) and restless moments as an "Inclination" graph (one spike each). Off by default. Old name: `--breathing-as-position`. |
| `--no-summary` | Don't write `garmin_nights_summary.csv` |
| `--no-journal` | Don't write `oscar_journal/garmin_journal.xml`. By default it is written for OSCAR's **File → Import Journal**: a note per night (scores, HRV, resting HR, stages, SpO2, breathing, Body Battery…) and bookmarks on breathing disruptions and the lowest SpO2. |
| `--no-day-before` | `garmin2oscar` only: don't fetch the activity of the day before each night |
| `--overwrite` | Start the cumulative files from scratch instead of adding to them |
| `--timezone Europe/Paris` | Time zone (default: the computer's) |
| `--min-minutes 30` | Ignore recordings shorter than this (daytime spot checks) |
| `--min-confidence N` | Drop readings below Garmin's confidence value N (see caveats) |
| `--offline` | `garmin2oscar` only: use cached `.fit` files, no login |
| `--no-movement` | `garmin2oscar` only: don't download the sleep movement |
| `--keep-dst-evening` | `garmin2oscar` only: don't drop pre-midnight readings on DST nights |

## Caveats

- **Accuracy.** This is a wrist sensor, not a medical oximeter. Treat dips with
  caution, especially perfectly flat plateaus, which often mean poor sensor contact.
- **Confidence value.** Garmin stores a "confidence" value with each reading, but
  it isn't documented, and on my data it didn't separate good readings from bad ones.
  Nothing is filtered by default.
- **Resolution.** One reading per minute, so brief desaturations are missed or smoothed.
- **Unofficial API.** `garmin2oscar_spo2.py` uses the unofficial `python-garminconnect`
  library, which uses Garmin Connect's private API. That's against Garmin's terms of
  use and can break at any time. `fit2oscar_spo2.py` with exported files is unaffected.
- **Sleep stages** are the watch's estimates (from heart rate and movement), not an EEG
  measurement like in a sleep lab.
- **Breathing disruptions** are Garmin's own estimate, only measured while Pulse Ox
  records. They are not apneas as scored by a CPAP.
- **Duplicates.** A night imported through both the wizard and the Viatom import
  appears twice in OSCAR. Purge one first.
- **Tested hardware.** Tested with a Garmin Venu 4 only. Feedback for other models is welcome.

## Privacy

Never commit your `.fit`, `.csv` or OSCAR files: they contain your health data.
The included `.gitignore` already excludes them.

## Disclaimer

Not affiliated with Garmin or the OSCAR team. Not a medical device. For
information only; talk to your doctor about your results.


