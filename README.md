# UPDATE:
for non geek: easy online version (you have to manualy export your garmin health fit data from: 
https://connect.garmin.com/app/settings/accountInformation
(at bottom)

then upload to:
https://mrbluematrix.github.io/garmin_to_oscar/

then unzip & import to OSCAR
done.

# garmin2oscar
<img width="2034" height="1048" alt="graph_pules_motion" src="https://github.com/user-attachments/assets/9abb0149-c313-4842-aa85-c65eb46b2148" />

Import the overnight **SpO2 ,pulse, motions** recorded by a Garmin watch into
[OSCAR](https://www.sleepfiles.com/OSCAR/), so you can see them next to your CPAP data.

OSCAR can't read Garmin `.fit` files. These scripts convert them into a format
OSCAR already supports (Wellue/Viatom O2Ring files, or ChoiceMMed MD300 `.dat`).

## Features

- Reads SpO2 readings (1 per minute during sleep) and pulse from Garmin wellness `.fit` files.
- Writes **one file per night**, imported all at once with OSCAR's
  **Import Viatom/Wellue data** (already-imported nights are skipped).
- Also writes a readable `.csv` per night (`Time,SpO2,Pulse,Confidence`).
- Handles daylight saving time. On a night that starts on a DST-change day,
  readings before midnight are dropped automatically, otherwise OSCAR labels
  that night's time axis 1 hour off (a display quirk in OSCAR).
- Optional automatic download from Garmin Connect, so no manual export is needed.

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

Prefer the Oximetry Wizard? Add `--format md300` to get ChoiceMMed `.dat` files.
Import them one night at a time with *Import from a data file* and choose
*use the oximeter's internal clock*. If you abandon a wizard import halfway,
restart OSCAR before the next one.

## Options

| Option | Effect |
|---|---|
| `--format viatom\|md300` | Output format (default `viatom`) |
| `--no-csv` | Don't write the `.csv` files |
| `--timezone Europe/Paris` | Time zone (default: the computer's) |
| `--min-minutes 30` | Ignore recordings shorter than this (daytime spot checks) |
| `--min-confidence N` | Drop readings below Garmin's confidence value N (see caveats) |
| `--offline` | `garmin2oscar` only: use cached `.fit` files, no login |
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
- **Duplicates.** A night imported through both the wizard and the Viatom import
  appears twice in OSCAR. Purge one first.
- **Tested hardware.** Tested with a Garmin Venu 4 only. Feedback for other models is welcome.

## Privacy

Never commit your `.fit`, `.csv` or OSCAR files: they contain your health data.
The included `.gitignore` already excludes them.

## Disclaimer

Not affiliated with Garmin or the OSCAR team. Not a medical device. For
information only; talk to your doctor about your results.
