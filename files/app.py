"""
Backend for the SQLMate login page.

This replaces the tkinter GUI in login_gui.py with a small Flask API that the
HTML/JS frontend (index.html) talks to over fetch(). The credential-checking
logic itself is copied over unchanged from login_gui.py's check_credentials():
same dictionary, same comparison, same pass/fail rule -- it's just triggered
by an HTTP request instead of a button inside a Tk window.

Run it with:
    pip install -r requirements.txt
    python app.py
Then open http://127.0.0.1:5000 in your browser.
"""

from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder=".", static_url_path="")

# Same dictionary of usernames -> passwords as login_gui.py
username_data = {
    "admin": "admin123",
    "john": "john456",
    "alice": "alicepass",
}


def check_credentials(username: str, password: str) -> bool:
    """Same rule as login_gui.py's check_credentials(): the username must
    exist in the dictionary and the password must match it exactly."""
    return username in username_data and username_data[username] == password


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/login", methods=["POST"])
def login():
    payload = request.get_json(silent=True) or {}
    username = payload.get("username", "")
    password = payload.get("password", "")

    if check_credentials(username, password):
        return jsonify(success=True)
    return jsonify(success=False)


if __name__ == "__main__":
    app.run(debug=True)
