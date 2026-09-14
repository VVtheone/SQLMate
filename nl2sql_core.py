from dotenv import load_dotenv
load_dotenv()

import os
import re
import json
import statistics
from datetime import datetime, date
from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Any, Dict

try:
    import mysql.connector
    from mysql.connector import Error as MySQLError
except ImportError:
    mysql = None
    MySQLError = Exception

try:
    from google import genai
except ImportError:
    genai = None

MIN_PROMPT_LENGTH = 8
GEMINI_MODEL = "gemini-3.1-flash-lite"  # swap for gemini-3.5-flash if you want the bigger model

DESTRUCTIVE_KEYWORDS = ("DELETE", "UPDATE", "DROP", "TRUNCATE", "ALTER", "INSERT")
DELETE_INTENT_WORDS = ("delete", "remove", "drop", "clear", "erase", "wipe")

AUDIT_LOG_PATH = os.path.join(os.path.expanduser("~"), ".nl2sql_audit.log")


@dataclass
class DBConnectResult:
    success: bool
    connection: Optional[Any] = None
    error: Optional[str] = None


@dataclass
class PromptValidation:
    valid: bool
    reason: Optional[str] = None


@dataclass
class QueryResult:
    success: bool
    columns: Optional[List[str]] = None
    rows: Optional[List[Tuple]] = None
    rowcount: Optional[int] = None
    is_select: bool = False
    error: Optional[str] = None
    last_insert_id: Optional[int] = None


@dataclass
class ConversationTurn:
    question: str
    sql: str


@dataclass
class DataSource:
    """The single connected table the assistant can read/write."""
    host: str
    database: str
    table: str
    schema: List[Tuple[str, str]]
    primary_key: Optional[str] = None


@dataclass
class GenerationResult:
    needs_clarification: bool
    question: Optional[str] = None
    sql: Optional[str] = None


@dataclass
class UndoAction:
    kind: str                        # "restore" or "insert_pending"
    description: str
    statements: List[Tuple[str, tuple]] = field(default_factory=list)
    table: Optional[str] = None
    pk_col: Optional[str] = None
    conn: Any = None


@dataclass
class AnalysisTurn:
    question: str
    answer: str


@dataclass
class AnalysisPlan:
    """What the data-analysis assistant decided to compute. The model only
    picks the plan (which columns, which kind of computation) - it never
    produces the actual numbers, so arithmetic mistakes aren't possible."""
    kind: str  # "stats", "trend", or "unsupported"
    columns: List[str] = field(default_factory=list)
    date_column: Optional[str] = None
    value_column: Optional[str] = None
    years_ahead: Optional[float] = None
    note: Optional[str] = None


def connect_to_database(host: str, db_user: str, db_password: str, database: str) -> DBConnectResult:
    if mysql is None:
        return DBConnectResult(success=False, error="mysql-connector-python is not installed.")
    try:
        conn = mysql.connector.connect(
            host=host, user=db_user, password=db_password, database=database
        )
        if conn.is_connected():
            return DBConnectResult(success=True, connection=conn)
        return DBConnectResult(success=False, error="Connection did not open.")
    except MySQLError as e:
        return DBConnectResult(success=False, error=str(e))


def validate_prompt(prompt: str, min_length: int = MIN_PROMPT_LENGTH) -> PromptValidation:
    #removes whitespace characters
    cleaned = prompt.strip()
    if len(cleaned) < min_length:
        return PromptValidation(valid=False, reason="Prompt is too short or vague.")
    return PromptValidation(valid=True)


def looks_like_delete(question: str) -> bool:
    lowered = question.lower()
    return any(word in lowered for word in DELETE_INTENT_WORDS)


def get_table_schema(conn, table_name: str, database: Optional[str] = None) -> List[Tuple[str, str]]:
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT COLUMN_NAME, DATA_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (database or conn.database, table_name),
        )
        return cursor.fetchall()
    finally:
        cursor.close()


def get_primary_key(conn, database: str, table_name: str) -> Optional[str]:
    """Gives primary key if it exists"""
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND CONSTRAINT_NAME = 'PRIMARY'
            ORDER BY ORDINAL_POSITION
            LIMIT 1
            """,
            (database, table_name),
        )
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        cursor.close()


def build_schema_context(source: DataSource) -> str:
    """Describe the connected table to the model."""
    cols = "\n".join(f"  - {name} ({dtype})" for name, dtype in source.schema)
    pk_note = f", primary key: {source.primary_key}" if source.primary_key else ""
    return f'Table `{source.database}`.`{source.table}` (host: {source.host}{pk_note}):\n{cols}'


def _numeric_columns_from_schema(schema: List[Tuple[str, str]]) -> List[str]:
    numeric_types = ("int", "decimal", "float", "double", "numeric", "real")
    return [name for name, dtype in schema if any(t in dtype.lower() for t in numeric_types)]


def plan_analysis(schema_context: str, question: str,
                   history: Optional[List[AnalysisTurn]] = None,
                   api_key: Optional[str] = None) -> AnalysisPlan:
    """Decide how to answer a data-analysis question: which computation to
    run and over which column(s). The model never computes the actual
    numbers - a separate Python step does that against the real data, so
    the model can't hallucinate an average or a projected value."""
    history_desc = ""
    if history:
        turns = "\n".join(
            f'  - You asked: "{t.question}" -> answered: "{t.answer}"' for t in history[-3:]
        )
        history_desc = f"\nRecent conversation, for follow-up questions:\n{turns}\n"

    prompt_text = f"""You are a data analyst working with this table:

{schema_context}
{history_desc}
The user asked: "{question}"

Decide how to answer using computed statistics - a separate step will
compute the exact numbers from the real data, so never estimate or invent
numbers yourself here, only decide the plan.

- If the question is about averages, medians, modes, totals/sums, min/max,
  spread (standard deviation), or counts of numeric column(s), respond with
  exactly:
  {{"kind": "stats", "columns": ["<numeric column name(s) relevant to the question>"]}}
- If the question asks how a numeric value will look, trend, grow, or
  project some number of years/months/days into the future based on
  recent/historical data, respond with exactly:
  {{"kind": "trend", "date_column": "<a date/datetime column in the table>", "value_column": "<the numeric column to project>", "years_ahead": <how many years ahead, as a float>}}
- If the question can't be answered from this table's columns, respond
  with exactly:
  {{"kind": "unsupported", "note": "<one short, plain-English reason>"}}

Respond with ONLY that JSON object - no markdown, no explanation."""

    raw = generate_sql(prompt_text, api_key=api_key)
    cleaned = raw.strip().strip("`")
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return AnalysisPlan(
            kind="unsupported",
            note="Couldn't figure out how to analyze that - try rephrasing.",
        )

    return AnalysisPlan(
        kind=data.get("kind", "unsupported"),
        columns=data.get("columns") or [],
        date_column=data.get("date_column"),
        value_column=data.get("value_column"),
        years_ahead=data.get("years_ahead"),
        note=data.get("note"),
    )


def _to_number(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compute_descriptive_stats(columns: List[str], rows: List[Tuple],
                               target_columns: List[str]) -> Dict[str, Dict[str, float]]:
    """Real arithmetic (statistics module) over the actual fetched rows -
    the model never sees or produces these numbers itself."""
    idx = {c: i for i, c in enumerate(columns)}
    results: Dict[str, Dict[str, float]] = {}
    for col in target_columns:
        if col not in idx:
            continue
        values = [_to_number(row[idx[col]]) for row in rows]
        values = [v for v in values if v is not None]
        if not values:
            continue
        modes = statistics.multimode(values)
        results[col] = {
            "count": len(values),
            "sum": sum(values),
            "mean": statistics.fmean(values),
            "median": statistics.median(values),
            "mode": modes[0] if len(modes) == 1 else modes,
            "min": min(values),
            "max": max(values),
            "stdev": statistics.pstdev(values) if len(values) > 1 else 0.0,
        }
    return results


def _to_ordinal(value: Any) -> Optional[float]:
    """Turn a date/datetime (or a common date-ish string) into a plain
    number of days, so a trend line can be fit against it."""
    if isinstance(value, datetime):
        return value.toordinal() + value.hour / 24
    if isinstance(value, date):
        return float(value.toordinal())
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y"):
            try:
                return float(datetime.strptime(value, fmt).toordinal())
            except ValueError:
                continue
    return None


def compute_trend_projection(columns: List[str], rows: List[Tuple],
                              date_col: str, value_col: str,
                              years_ahead: float) -> Optional[Dict[str, Any]]:
    """Fit a simple least-squares line through (date, value) and
    extrapolate it years_ahead years past the latest date in the data."""
    idx = {c: i for i, c in enumerate(columns)}
    if date_col not in idx or value_col not in idx:
        return None

    points = []
    for row in rows:
        x = _to_ordinal(row[idx[date_col]])
        y = _to_number(row[idx[value_col]])
        if x is not None and y is not None:
            points.append((x, y))
    if len(points) < 2:
        return None

    points.sort(key=lambda p: p[0])
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    n = len(points)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    if ss_xx == 0:
        return None  # every row has the same date - nothing to fit a trend to
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    slope = ss_xy / ss_xx
    intercept = mean_y - slope * mean_x

    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in points)
    r_squared = (1 - ss_res / ss_tot) if ss_tot else None

    last_x = xs[-1]
    future_x = last_x + years_ahead * 365.25
    projected = slope * future_x + intercept

    return {
        "date_column": date_col,
        "value_column": value_col,
        "n_data_points": n,
        "earliest_date": date.fromordinal(int(xs[0])).isoformat(),
        "latest_date": date.fromordinal(int(xs[-1])).isoformat(),
        "latest_value": ys[-1],
        "average_change_per_year": slope * 365.25,
        "years_ahead": years_ahead,
        "projected_value": projected,
        "fit_quality_r_squared": r_squared,
        "method": ("simple linear regression over the full history - a "
                   "straight-line extrapolation, not a guarantee"),
    }


def answer_from_computation(question: str, computation: Dict[str, Any],
                             api_key: Optional[str] = None) -> str:
    prompt_text = f"""The user asked: "{question}"

These exact statistics were computed from the real data - use only these
numbers, don't recompute or second-guess them:
{json.dumps(computation, indent=2, default=str)}

Answer the user's question in 2-4 plain-English sentences, referencing the
relevant numbers (round sensibly). If this includes a future projection,
make clear it's a straight-line estimate based on the recent trend, not a
guarantee. No preamble, no markdown."""
    return generate_sql(prompt_text, api_key=api_key)


def generate_sql(prompt_text: str, api_key: Optional[str] = None) -> str:
    if genai is None:
        raise RuntimeError(
            "google-genai is not installed. Run:\n"
            "  pip install google-genai --break-system-packages"
        )
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set.")

    client = genai.Client(api_key=key)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt_text,
    )

    text = response.text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json\n") or text.lower().startswith("sql\n"):
            text = text[4:]
        text = text.strip()
    return text


def generate_sql_or_clarify(schema_context: str, user_question: str,
                             history: Optional[List[ConversationTurn]] = None,
                             api_key: Optional[str] = None) -> GenerationResult:
    """Single model call that either returns SQL or, if the request is
    ambiguous, a clarifying question - one round trip instead of a separate
    'is this clear' pass followed by a separate generation pass."""
    history_desc = ""
    if history:
        turns = "\n".join(
            f'  - You asked: "{t.question}" -> generated: {t.sql}' for t in history[-3:]
        )
        history_desc = f"\nRecent conversation, for follow-up questions:\n{turns}\n"

    prompt_text = f"""You are a MySQL expert working against these tables:

{schema_context}
{history_desc}
Request: "{user_question}"

Guidance on intent:
- "add a row" / "insert" -> INSERT INTO ...
- "add N to <column>" / "increase <column> by N" -> UPDATE ... SET column = column + N WHERE ...
- "subtract N from <column>" / "decrease <column> by N" -> UPDATE ... SET column = column - N WHERE ...
- "delete/remove this row(s)" -> DELETE FROM ... WHERE ...
- "clear/delete the value in <column> for ..." -> UPDATE ... SET column = NULL WHERE ...
- "delete/remove the <column> column" -> ALTER TABLE ... DROP COLUMN ...
- Always include a WHERE clause for UPDATE/DELETE that targets specific row(s) -
  never write an unqualified UPDATE/DELETE unless the user clearly asked for
  the whole table.

Decide first whether the request is unambiguous enough to safely turn into ONE
SQL statement - you must be sure which table, which column(s), and (for any
change) which row(s) it applies to.

- If it is unambiguous, respond with exactly:
  {{"needs_clarification": false, "sql": "<the single SQL statement>"}}
- If it is ambiguous (unclear table, column, or which row(s) a change applies
  to, or too vague to act on), respond with exactly:
  {{"needs_clarification": true, "question": "<one short, specific question>"}}

Respond with ONLY that JSON object - no markdown, no explanation."""

    raw = generate_sql(prompt_text, api_key=api_key)
    cleaned = raw.strip().strip("`")
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Model didn't return clean JSON - fall back to treating the raw
        # text as SQL, which is what the rest of the app already expects.
        return GenerationResult(needs_clarification=False, sql=cleaned)

    if data.get("needs_clarification"):
        return GenerationResult(needs_clarification=True, question=data.get("question"))
    return GenerationResult(needs_clarification=False, sql=(data.get("sql") or "").strip())


def is_destructive_query(sql: str) -> bool:
    first_word = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
    return first_word in DESTRUCTIVE_KEYWORDS


def explain_sql(sql: str, api_key: Optional[str] = None) -> str:
    prompt_text = f"""Explain what this MySQL query does, in one or two plain-English
sentences a non-technical person could understand. No preamble, no markdown.

{sql}"""
    return generate_sql(prompt_text, api_key=api_key)


def summarize_results(user_question: str, columns: List[str], rows: List[Tuple],
                       api_key: Optional[str] = None) -> str:
    preview_rows = rows[:20]
    rows_desc = "\n".join(", ".join(str(v) for v in row) for row in preview_rows)
    prompt_text = f"""The user asked: "{user_question}"

The query returned these columns: {", ".join(columns)}
And these rows (possibly truncated):
{rows_desc}

Total rows returned: {len(rows)}

Summarize the result in one or two plain-English sentences. No preamble, no markdown."""
    return generate_sql(prompt_text, api_key=api_key)


def diagnose_error(sql: str, error_message: str, api_key: Optional[str] = None) -> str:
    prompt_text = f"""This MySQL query failed:
{sql}

With this error:
{error_message}

In two or three plain-English sentences, explain what likely went wrong and
suggest a concrete fix. No preamble, no markdown."""
    return generate_sql(prompt_text, api_key=api_key)


def _split_sql_statements(sql: str) -> List[str]:
    """Split a string of one or more semicolon-separated SQL statements,
    ignoring semicolons that appear inside quoted string literals.

    mysql-connector-python's cursor.execute() only accepts ONE statement
    per call - handing it a compound string like "DELETE ...; INSERT ...;"
    directly either errors out or (worse) can raise something that isn't a
    MySQLError, which leaves the connection/cursor in a bad state. Splitting
    here lets the caller execute each statement with its own execute() call.
    """
    statements = []
    current = []
    quote_char = None
    for ch in sql:
        if quote_char:
            current.append(ch)
            if ch == quote_char:
                quote_char = None
            continue
        if ch in ("'", '"', "`"):
            quote_char = ch
            current.append(ch)
            continue
        if ch == ";":
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
            continue
        current.append(ch)
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def run_query(conn, sql: str) -> QueryResult:
    statements = _split_sql_statements(sql)
    if not statements:
        return QueryResult(success=False, error="No SQL statement to run.")

    cursor = conn.cursor()
    try:
        if len(statements) == 1:
            cursor.execute(statements[0])
            if cursor.description:  # SELECT
                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                return QueryResult(success=True, columns=columns, rows=rows, is_select=True)
            conn.commit()
            return QueryResult(
                success=True, rowcount=cursor.rowcount, is_select=False,
                last_insert_id=(cursor.lastrowid or None),
            )

        # Multiple statements (e.g. "delete the old row; insert the new
        # one") - run them one at a time as a single all-or-nothing
        # transaction, since executing a compound string in one call isn't
        # supported.
        total_rowcount = 0
        last_insert_id = None
        for stmt in statements:
            cursor.execute(stmt)
            if cursor.description:
                cursor.fetchall()  # discard - not expected mid-batch here
            else:
                total_rowcount += cursor.rowcount
                if cursor.lastrowid:
                    last_insert_id = cursor.lastrowid
        conn.commit()
        return QueryResult(
            success=True, rowcount=total_rowcount, is_select=False,
            last_insert_id=last_insert_id,
        )
    except MySQLError as e:
        try:
            conn.rollback()
        except MySQLError:
            pass
        return QueryResult(success=False, error=str(e))
    finally:
        cursor.close()


def log_audit(question: str, sql: str, result: QueryResult) -> None:
    """Append every executed statement to a local audit log. Best-effort -
    a logging failure should never block the user's actual work."""
    try:
        outcome = (
            f"{len(result.rows)} row(s) returned" if result.is_select
            else f"{result.rowcount} row(s) affected"
        )
        with open(AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            ts = datetime.now().isoformat(timespec="seconds")
            f.write(f"[{ts}] Q: {question!r}\nSQL: {sql}\nOutcome: {outcome}\n\n")
    except OSError:
        pass


def _parse_target_and_where(sql: str) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort extraction of the table name and WHERE clause text from
    an UPDATE/DELETE statement, used to build a preview/backup SELECT."""
    m = re.search(r'UPDATE\s+([`"\w.]+)', sql, re.IGNORECASE)
    if not m:
        m = re.search(r'DELETE\s+FROM\s+([`"\w.]+)', sql, re.IGNORECASE)
    table = m.group(1).strip() if m else None

    where_match = re.search(r'\bWHERE\b(.+?)(?:\bLIMIT\b|\bORDER BY\b|$)', sql, re.IGNORECASE | re.DOTALL)
    where_clause = where_match.group(1).strip() if where_match else None
    return table, where_clause


def build_preview_query(sql: str) -> Optional[str]:
    """For UPDATE/DELETE, build a SELECT that shows which rows will be hit -
    so the confirmation dialog can say 'this affects N rows' instead of just
    showing the raw SQL text."""
    op = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
    if op not in ("UPDATE", "DELETE"):
        return None
    table, where_clause = _parse_target_and_where(sql)
    if not table:
        return None
    if where_clause:
        return f"SELECT * FROM {table} WHERE {where_clause} LIMIT 50"
    return f"SELECT * FROM {table} LIMIT 50"