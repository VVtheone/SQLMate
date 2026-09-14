# SQLMate

Ask your MySQL database questions in plain English; SQLMate turns them into
SQL, runs them, and summarizes the results - all from one browser tab.

## Files (this is everything - 4 files total)

- **`main.py`** - the one thing you run. It's the Flask backend (DB connect,
  the ask -> generate -> confirm -> execute flow, etc.) *and* the launcher:
  it starts the server and opens your browser for you.
- **`nl2sql_core.py`** - the framework-agnostic logic `main.py` calls into
  (DB connection, SQL generation via Gemini, query execution, summarization).
- **`frontend.html`** - the whole UI: a login screen and the dashboard, in
  one page. `main.py` serves this at `/`.
- **`requirements.txt`**, **`.env.example`** - setup.

Everything else from the old project (the separate `app.py`, the Tkinter
`login_gui.py` / `nl2sql_gui_app.py` desktop app, the standalone
`loginpartfinal.html` login mockup, and the old single-file
`sqlmate_app.py`) has been folded into these four files, since the app is
now web-only with one entry point.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then put your real GEMINI_API_KEY in .env
python main.py
```

Your browser opens automatically to **http://127.0.0.1:5000**. If it
doesn't, open that URL yourself.

## Logging in

The dashboard is behind a simple login (checked server-side in `main.py`,
not just in the browser). Default accounts, defined in the `USERS` dict at
the top of `main.py`:

| Username        | Password         |
|-----------------|------------------|
| `admin`         | `admin123`       |
| `bananaboy123`  | `ihavebigbanana` |

Edit that dict to add, remove, or change accounts. Three wrong attempts
locks logins out for 60 seconds, same as the old desktop app.

## Using it

1. Fill in Host/User/Password/Database/Table and click **Connect**.
2. Type a question in **Ask about your data** and click **Analyze** (or
   press Enter).
3. If your question is ambiguous, the assistant asks a follow-up in the
   Assistant panel - just answer it in the same box.
4. If your question sounds like it deletes or clears something, you'll get
   a quick prompt asking exactly what it should affect before any SQL is
   even generated.
5. For anything destructive (UPDATE/DELETE/DROP/etc.), you'll see the SQL,
   an explanation, and how many rows it affects before you confirm it runs.
6. Results show up in the table on the right; the Assistant panel gives you
   a plain-English summary.
7. Underneath the Assistant panel is a separate **Data Analysis Chat** - ask
   it things like "what's the average and median salary" or "project
   revenue 2 years from now". It computes mean/median/mode/sum/min/max/
   standard deviation, and future-value projections (a least-squares trend
   fit), with real arithmetic in Python - the AI only ever picks *which*
   computation to run and phrases the answer, it never invents the numbers.
   This is a separate conversation from the SQL assistant above it.

## Notes

- Single-connection model: one table connected at a time, held in the
  server process - fine for one person running this locally, not meant for
  multiple simultaneous users.
- This runs on `127.0.0.1` (your machine only) by default. Don't expose it
  to the open internet without adding HTTPS and a real auth system in
  front of it - the built-in login is convenient, not hardened.
