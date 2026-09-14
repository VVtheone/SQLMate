import os
import threading
import time
import webbrowser
from functools import wraps
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, request, send_from_directory, session

import nl2sql_core as core

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=None)
# A fresh random key each run is fine here: it just means everyone has to
# log back in if you restart the server, which is the behavior you want for
# a local tool like this.
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(24).hex()

_lock = threading.RLock()

state: Dict[str, Any] = {
    "connection": None,
    "data_source": None,     # core.DataSource
    "history": [],           # List[core.ConversationTurn]  (SQL-generation chat)
    "analysis_history": [],  # List[core.AnalysisTurn]      (stats/projection chat)
    "last_view_sql": None,
}

# Cap on how many rows the analysis chat pulls in for its arithmetic - keeps
# a "just compute it" question from trying to load a huge table into memory.
ANALYSIS_ROW_LIMIT = 50000

DELETE_LABEL_MAP = {
    "row": "delete whole row(s)",
    "value": "clear a single value (set it to NULL), not the whole row",
    "column": "drop an entire column from the table",
}


#Login
USERS = {
    "admin": "admin123",
    "bananaboy123": "ihavebigbanana",
}
MAX_ATTEMPTS = 3
LOCKOUT_SECONDS = 60

_auth_lock = threading.RLock()
_login_attempts = {"count": 0, "locked_until": 0.0}


def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"error": "Not authenticated."}), 401
        return fn(*args, **kwargs)
    return wrapper


@app.route("/api/session", methods=["GET"])
def api_session():
    return jsonify({"authenticated": bool(session.get("authenticated"))})


@app.route("/api/login", methods=["POST"])
def api_login():
    with _auth_lock:
        now = time.time()
        remaining = _login_attempts["locked_until"] - now
        if remaining > 0:
            return jsonify({
                "success": False,
                "error": f"Too many attempts. Try again in {int(remaining) + 1}s.",
            }), 429

        body = request.get_json(force=True) or {}
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""

        if username in USERS and USERS[username] == password:
            _login_attempts["count"] = 0
            _login_attempts["locked_until"] = 0.0
            session["authenticated"] = True
            session["username"] = username
            return jsonify({"success": True})

        _login_attempts["count"] += 1
        if _login_attempts["count"] > MAX_ATTEMPTS:
            _login_attempts["locked_until"] = now + LOCKOUT_SECONDS
            _login_attempts["count"] = 0
            return jsonify({
                "success": False,
                "error": f"Too many attempts. Locked for {LOCKOUT_SECONDS} seconds.",
            }), 429

        return jsonify({"success": False, "error": "Incorrect username or password."}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True})

#Helpers

def _jsonify_value(v: Any) -> Any:
    """MySQL rows can contain Decimal/datetime/date/bytes/etc, none of which
    json.dumps can handle directly. Everything the table just needs to
    *display* is fine as a string; keep plain JSON-safe scalars as-is."""
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


def _serialize_rows(rows: Optional[List[Tuple]]) -> List[List[Any]]:
    if not rows:
        return []
    return [[_jsonify_value(v) for v in row] for row in rows]


def _query_result_json(result: "core.QueryResult") -> Dict[str, Any]:
    return {
        "success": result.success,
        "columns": result.columns or [],
        "rows": _serialize_rows(result.rows),
        "rowcount": result.rowcount,
        "is_select": result.is_select,
        "error": result.error,
    }


def _not_connected():
    return jsonify({"error": "Not connected to a table. Connect to a database table first."}), 400


def _connected() -> bool:
    return state["connection"] is not None and state["data_source"] is not None


#static frontend

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "frontend.html")


#connection creation
@app.route("/api/state", methods=["GET"])
@require_auth
def api_state():
    with _lock:
        ds = state["data_source"]
        if not ds:
            return jsonify({"connected": False})
        return jsonify({
            "connected": True,
            "alias": f"{ds.database}.{ds.table}",
            "host": ds.host,
            "database": ds.database,
            "table": ds.table,
            "primary_key": ds.primary_key,
            "columns": [{"name": n, "type": t} for n, t in ds.schema],
        })


@app.route("/api/connect", methods=["POST"])
@require_auth
def api_connect():
    body = request.get_json(force=True) or {}
    host = (body.get("host") or "").strip() or "localhost"
    db_user = (body.get("user") or "").strip()
    db_password = body.get("password") or ""
    database = (body.get("database") or "").strip()
    table = (body.get("table") or "").strip()

    if not database or not table:
        return jsonify({"success": False, "error": "Enter a database and table name."}), 400

    result = core.connect_to_database(host, db_user, db_password, database)
    if not result.success:
        return jsonify({"success": False, "error": result.error}), 400

    conn = result.connection
    try:
        schema = core.get_table_schema(conn, table, database=database)
        primary_key = core.get_primary_key(conn, database, table)
    except core.MySQLError as e:
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({"success": False, "error": str(e)}), 400

    if not schema:
        try:
            conn.close()
        except Exception:
            pass
        return jsonify({
            "success": False,
            "error": f"'{table}' has no columns (or does not exist) in '{database}'.",
        }), 400

    with _lock:
        if state["connection"] is not None:
            try:
                state["connection"].close()
            except Exception:
                pass
        state["connection"] = conn
        state["data_source"] = core.DataSource(
            host=host, database=database, table=table,
            schema=schema, primary_key=primary_key,
        )
        state["history"] = []
        state["analysis_history"] = []
        state["last_view_sql"] = None

    alias = f"{database}.{table}"
    return jsonify({
        "success": True,
        "alias": alias,
        "host": host,
        "primary_key": primary_key,
        "columns": [{"name": n, "type": t} for n, t in schema],
    })


@app.route("/api/disconnect", methods=["POST"])
@require_auth
def api_disconnect():
    with _lock:
        if state["connection"] is not None:
            try:
                state["connection"].close()
            except Exception:
                pass
        state["connection"] = None
        state["data_source"] = None
        state["history"] = []
        state["analysis_history"] = []
        state["last_view_sql"] = None
    return jsonify({"success": True})


#operators used by frontend
@app.route("/api/validate", methods=["POST"])
@require_auth
def api_validate():
    body = request.get_json(force=True) or {}
    question = body.get("question") or ""
    v = core.validate_prompt(question)
    return jsonify({"valid": v.valid, "reason": v.reason})


@app.route("/api/looks_like_delete", methods=["POST"])
@require_auth
def api_looks_like_delete():
    body = request.get_json(force=True) or {}
    question = body.get("question") or ""
    return jsonify({"is_delete": core.looks_like_delete(question)})


@app.route("/api/format_delete_clarification", methods=["POST"])
@require_auth
def api_format_delete_clarification():
    body = request.get_json(force=True) or {}
    question = body.get("question") or ""
    choice = body.get("choice") or ""
    location = body.get("location") or ""
    label = DELETE_LABEL_MAP.get(choice)
    if not label or not location:
        return jsonify({"error": "Missing choice/location."}), 400
    merged = f"{question}\n\n[Clarified: the user wants to {label}; target: {location}]"
    return jsonify({"question": merged})


# ai checking and clarifying
@app.route("/api/generate", methods=["POST"])
@require_auth
def api_generate():
    body = request.get_json(force=True) or {}
    question = body.get("question") or ""

    with _lock:
        if not _connected():
            return _not_connected()
        data_source = state["data_source"]
        conn = state["connection"]
        history = list(state["history"])

    schema_context = core.build_schema_context(data_source)

    try:
        result = core.generate_sql_or_clarify(schema_context, question, history=history)
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    if result.needs_clarification:
        return jsonify({"needs_clarification": True, "question": result.question})

    if not result.sql:
        return jsonify({
            "needs_clarification": False,
            "sql": None,
            "error": "The assistant didn't return a usable query - try rephrasing.",
        })

    sql = result.sql

    try:
        explanation = core.explain_sql(sql)
    except RuntimeError:
        explanation = None

    preview_sql = core.build_preview_query(sql)
    preview_json = None
    if preview_sql:
        with _lock:
            conn = state["connection"]
            if conn is not None:
                preview = core.run_query(conn, preview_sql)
                preview_json = _query_result_json(preview)

    multi_statement = len(core._split_sql_statements(sql)) > 1
    destructive = (not multi_statement) and core.is_destructive_query(sql)

    return jsonify({
        "needs_clarification": False,
        "sql": sql,
        "explanation": explanation,
        "multi_statement": multi_statement,
        "destructive": destructive,
        "preview": preview_json,
    })


@app.route("/api/execute", methods=["POST"])
@require_auth
def api_execute():
    body = request.get_json(force=True) or {}
    question = body.get("question") or ""
    sql = body.get("sql") or ""

    if not sql.strip():
        return jsonify({"success": False, "error": "No SQL statement to run."}), 400

    with _lock:
        if not _connected():
            return _not_connected()
        conn = state["connection"]

    result = core.run_query(conn, sql)

    if not result.success:
        diagnosis = None
        try:
            diagnosis = core.diagnose_error(sql, result.error)
        except RuntimeError:
            pass
        return jsonify({
            "success": False,
            "error": result.error,
            "diagnosis": diagnosis,
        })

    core.log_audit(question, sql, result)

    with _lock:
        state["history"].append(core.ConversationTurn(question=question, sql=sql))
        if result.is_select:
            state["last_view_sql"] = sql

    payload = _query_result_json(result)

    if result.is_select:
        try:
            payload["summary"] = core.summarize_results(question, result.columns, result.rows)
        except RuntimeError:
            payload["summary"] = None

    return jsonify(payload)


@app.route("/api/analysis", methods=["POST"])
@require_auth
def api_analysis():
    """Separate chat from the SQL ask-flow above: mean/median/mode/sum/etc.
    and future-value trend projections, computed with real arithmetic
    (statistics module / a least-squares fit) over the actual table data -
    the model only ever picks *which* computation to run, never the numbers
    themselves, so it can't hallucinate a stat or a projection."""
    body = request.get_json(force=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Enter a question."}), 400

    with _lock:
        if not _connected():
            return _not_connected()
        data_source = state["data_source"]
        history = list(state["analysis_history"])

    schema_context = core.build_schema_context(data_source)

    try:
        plan = core.plan_analysis(schema_context, question, history=history)
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400

    def _remember(answer: str) -> None:
        with _lock:
            state["analysis_history"].append(core.AnalysisTurn(question=question, answer=answer))

    if plan.kind == "unsupported":
        answer = plan.note or "I can't answer that from this table's columns."
        _remember(answer)
        return jsonify({"kind": "unsupported", "answer": answer, "computation": None})

    with _lock:
        if not _connected():
            return _not_connected()
        conn = state["connection"]
        fetch_sql = f"SELECT * FROM `{data_source.database}`.`{data_source.table}` LIMIT {ANALYSIS_ROW_LIMIT}"
        fetch_result = core.run_query(conn, fetch_sql)

    if not fetch_result.success:
        return jsonify({"error": fetch_result.error}), 400

    columns, rows = fetch_result.columns, fetch_result.rows

    if plan.kind == "stats":
        stats = core.compute_descriptive_stats(columns, rows, plan.columns or [])
        if not stats:
            answer = "I couldn't find usable numeric data in that column - double-check the column name."
            _remember(answer)
            return jsonify({"kind": "stats", "answer": answer, "computation": None})
        computation = {"stats": stats}
        try:
            answer = core.answer_from_computation(question, computation)
        except RuntimeError as e:
            answer = f"Computed the stats, but couldn't phrase an answer ({e})."
        _remember(answer)
        return jsonify({"kind": "stats", "answer": answer, "computation": computation})

    if plan.kind == "trend":
        if not plan.date_column or not plan.value_column:
            answer = "I need a clear date column and a numeric column to project - try naming both."
            _remember(answer)
            return jsonify({"kind": "trend", "answer": answer, "computation": None})

        years_ahead = plan.years_ahead if plan.years_ahead else 1.0
        projection = core.compute_trend_projection(
            columns, rows, plan.date_column, plan.value_column, years_ahead
        )
        if not projection:
            answer = ("I couldn't fit a trend there - make sure that date column has "
                       "enough distinct dates and the value column is numeric.")
            _remember(answer)
            return jsonify({"kind": "trend", "answer": answer, "computation": None})
        try:
            answer = core.answer_from_computation(question, projection)
        except RuntimeError as e:
            answer = f"Computed the projection, but couldn't phrase an answer ({e})."
        _remember(answer)
        return jsonify({"kind": "trend", "answer": answer, "computation": projection})

    return jsonify({"error": "Unrecognized analysis plan."}), 400


@app.route("/api/refresh_view", methods=["POST"])
@require_auth
def api_refresh_view():
    with _lock:
        if not _connected():
            return _not_connected()
        conn = state["connection"]
        data_source = state["data_source"]
        view_sql = state["last_view_sql"]

    if not view_sql and data_source:
        view_sql = f"SELECT * FROM `{data_source.database}`.`{data_source.table}` LIMIT 200"
    if not view_sql:
        return jsonify({"columns": [], "rows": []})

    result = core.run_query(conn, view_sql)
    if result.success and result.is_select:
        return jsonify({"columns": result.columns, "rows": _serialize_rows(result.rows)})
    return jsonify({"columns": [], "rows": []})


#opens browser video
def _open_browser(url: str) -> None:
    time.sleep(1.0)
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    url = f"http://127.0.0.1:{port}"
    threading.Thread(target=_open_browser, args=(url,), daemon=True).start()
    print(f"SQLMate is running at {url}  (Ctrl+C to stop)")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
