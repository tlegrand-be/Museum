# Roster Ledger

A small dashboard for a museum roster: upload a photo of the day's staff
schedule, Gemini reads the names and positions off it, you confirm/correct
them, and they get saved. From there you get per-colleague stats, a
per-location ranking, charts, and an Excel export.

## How it works

1. **Upload** — pick a date and a photo of the written roster.
2. **Gemini reads it** — extracts a list of `{name, position}` pairs.
3. **Review** — you see an editable table before anything is saved, since
   handwriting reads aren't always perfect.
4. **Saved to SQLite** — the real database (`instance/museum.db`). Excel is
   generated on demand from this data, not used as the storage itself, so
   nothing breaks as the data grows.
5. **Dashboard** — overview stats, click any colleague to see where/when they
   worked, click any location to see who has worked there most.

## Local setup

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Get a free Gemini API key at https://aistudio.google.com/apikey, then set it:

```bash
export GEMINI_API_KEY="your-key-here"
export FLASK_SECRET_KEY="something-random"
```

(Or copy `.env.example` to `.env` and load it with your usual method — the
app itself reads these from `os.environ`, so any way of setting them works.)

Run it:

```bash
python app.py
```

Visit http://localhost:5000

## Deploying to PythonAnywhere

1. Upload the whole `museum-dashboard` folder (via the Files tab, or `git
   clone` if you push this to a repo).
2. Open a **Bash console** on PythonAnywhere and install dependencies:
   ```bash
   cd museum-dashboard
   pip install --user -r requirements.txt
   ```
3. Go to the **Web** tab → **Add a new web app** → choose **Flask** → point
   it at `museum-dashboard/app.py`.
4. In the **Web** tab, under **Environment variables**, add:
   - `GEMINI_API_KEY` = your key
   - `FLASK_SECRET_KEY` = any random string
5. Under **Code**, make sure the working directory is set to the
   `museum-dashboard` folder so `instance/` and `uploads/` resolve correctly.
6. Hit **Reload**.

Free-tier PythonAnywhere accounts can reach external APIs like Gemini's
`generativelanguage.googleapis.com` — this isn't the Yahoo Finance situation
from your other project, so no extra whitelisting should be needed. If a
request ever fails with a network error, double check the **Web tab →
Whitelisted domains** section for your account tier.

## Notes / things you can extend later

- Currently one shift = one (worker, location, date) row, so you can't log
  the same person at the same location twice on the same day — re-uploads
  for the same date/location/person are silently skipped rather than
  duplicated.
- If your museum ever wants half-day shifts (morning/afternoon), add a
  `shift_period` column to the `shifts` table and to the review form.
- The `.xlsx` export includes a raw data sheet plus per-worker and
  per-location summary sheets.
