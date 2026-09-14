# SQLMate Login

The frontend (`index.html`) is now wired to a real Python backend (`app.py`)
instead of checking passwords in JavaScript. `app.py` reuses the exact
credential dictionary and comparison logic from `login_gui.py` — it's the
same check, just served over HTTP so the browser can call it.

## Run it

```bash
pip install -r requirements.txt
python app.py
```

Then open **http://127.0.0.1:5000** in your browser.

## What changed from the files you uploaded

- **`login_gui.py`'s logic → `app.py`**: the `username_data` dict and the
  "username exists and password matches" check are now behind a
  `POST /api/login` endpoint that returns `{"success": true|false}`.
- **`index.html`**: `checkCredentials()` now calls that endpoint with
  `fetch()` instead of checking a copy of the dictionary sitting in the
  page's own JavaScript. All the existing animations (shake on failure,
  dot-morph-to-checkmark on success, glass expand transitions) are unchanged
  and still run client-side — only the actual verification moved to Python.
- Fixed a bug where the previous-login buttons relied on the implicit global
  `window.event`; they now pass the event in explicitly.
- Added a **John** button (the third account from `login_gui.py`, previously
  missing from "Previous logins").
- Added a working **Log out** button on the "Inside" screen, and the submit
  button now disables itself while a request is in flight so double-clicking
  can't fire two logins at once.

## Test accounts

| Username | Password    |
|----------|-------------|
| admin    | admin123    |
| john     | john456     |
| alice    | alicepass   |
