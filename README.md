# garmin2oscar

Import the overnight **SpO2 and pulse** recorded by a Garmin watch into
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
- **Night app recordings**: an activity recorded during the night by an app that logs
  SpO2 and pulse every second (developer fields) and the accelerometer is converted too.
  SpO2 and pulse go to OSCAR every 2 s with a movement index (from the 100 Hz
  accelerometer) in the Movement graph, and movement-based **micro-awakenings and
  awakenings** are detected (Journal note, summary, `garmin_night_*_awakenings.csv`, and
  the Somnopose Inclination graph with `--somnopose`). `garmin2oscar_spo2.py` downloads
  these activities automatically (started between 18:00 and noon, 2 h or more;
  `--no-night-app` to skip). This is an actigraphy estimate, not an EEG arousal count.
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

## Web page: convert or just look

The web page (`index.html`, also usable on GitHub Pages) has two modes, chosen at the top:

- **Convert for OSCAR**: the files described below, in one download.
- **View my nights**: nothing to download. Pick a night to see its Journal note and its
  graphs on one time axis: SpO2, pulse, movement, sleep stages, body position (watch on the
  chest), breathing disruptions, and micro-awakenings / awakenings or restless moments.

Everything stays in your browser.

## Installation

```bash
git clone https://github.com/YOUR-USERNAME/garmin2oscar.git
cd garmin2oscar
./setup.sh
```

`setup.sh` installs everything **in this folder**: a Python environment (`.venv`, Python 3.12+
needed: `brew install python@3.13` on a Mac) with `garminconnect`, and
[airbreak-plus](https://github.com/m-kozlowski/airbreak-plus) for the optional AirSense 11
clock correction. Run it again later to update.

Then use the short command `./g2o`:

```bash
./g2o                  # last night
./g2o --nights 7       # the last 7 nights
./g2o --airsense       # ... with the AirSense 11 clock correction
```

For `--airsense`, pair the machine once (`setup.sh` shows the two commands); the paired
machine is then found automatically.

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
6. For the Journal (a note per night), **follow these steps exactly**, because OSCAR's
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

### Sleep position with the watch on the chest (optional)

If you tape the watch on your chest (screen out, top towards the head) while the night
app records, `--chest` turns the accelerometer into a sleep position: OSCAR's
**Inclination** graph shows the roll angle (0 = back, +90 = left side, −90 = right side,
±180 = belly; needs `--somnopose`), and the Journal note gets a line such as
`Position: back 4h10 (52 %) · left 2h05 (26 %) · right 1h45 (22 %) · 14 changes`.
Use `--chest-flip` if left and right come out swapped. On those nights, the movement-based
awakenings stay in the Journal, the summary and the CSV files.

### Lining up with a ResMed AirSense 11 clock (optional)

CPAP clocks drift. With `--airsense`, the script reads the AirSense 11 clock over
Bluetooth (read only, `GetDateTime`), compares it with the computer's clock and shifts
**every Garmin time** by the difference, so the Garmin data lines up with the CPAP data
in OSCAR. It uses [airbreak-plus](https://github.com/m-kozlowski/airbreak-plus), installed by
`setup.sh`, and the machine paired with it (found automatically when only one is paired).

```bash
./g2o --airsense
```

The device and the airbreak-plus folder are saved in `~/.garmin2oscar.json`, with the last
measured offset, used if the machine can't be reached. `--clock-offset SECONDS` applies a
known offset without Bluetooth. `airsense_clock_check.py` can also be run on its own to
just display the offset.

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
| `--no-journal` | Don't write `oscar_journal/garmin_journal.xml`. By default it is written for OSCAR's **File → Import Journal**: a note per night (scores, HRV, resting HR, stages, SpO2, breathing, Body Battery…) |
| `--no-night-app` | `garmin2oscar` only: don't download night activities recorded by a SpO2/pulse/motion app |
| `--no-day-before` | `garmin2oscar` only: don't fetch the activity of the day before each night |
| `--airsense [DEVICE]` | Read the AirSense 11 clock over Bluetooth and shift every Garmin time by its offset. Off by default; DEVICE is saved after the first use |
| `--airbreak PATH` | airbreak-plus folder (saved too) |
| `--chest` | The watch is taped on the chest during the night app recording: the body position (back / left / right / belly / upright) goes to OSCAR's Inclination graph (roll angle), the Journal, the summary and `garmin_night_*_position.csv`. Off by default |
| `--chest-flip` | With `--chest`: the watch is upside down (swaps left and right) |
| `--clock-offset SECONDS` | Shift every Garmin time by this offset, without Bluetooth (CPAP ahead > 0) |
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
