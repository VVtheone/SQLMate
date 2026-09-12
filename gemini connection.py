#!/usr/bin/env python3
"""
Natural-Language-to-SQL Assistant (no login)
==============================================
Same flow as before, minus the account system: straight to the
database connection, then the ask -> validate -> generate SQL ->
explain -> confirm -> run -> summarize loop, with an AI assistant
layer (explanations, destructive-query warnings, result summaries,
error diagnosis, and short conversation memory for follow-ups).

Requirements:
    pip install mysql-connector-python google-generativeai --break-system-packages

Set your Gemini API key as an environment variable before running:
    export GEMINI_API_KEY="your-key-here"

Run:
    python3 nl2sql_assistant_no_login.py
"""

import os
import sys
from dataclasses import dataclass
from typing import List, Tuple, Optional, Any

try:
    import mysql.connector
    from mysql.connector import Error as MySQLError
except ImportError:
    mysql = None
    MySQLError = Exception

try:
    import google.generativeai as genai
except ImportError:
    genai = None


# =============================================================== #
# ===========================  BACKEND  ========================== #
# =============================================================== #

MIN_PROMPT_LENGTH = 8
GEMINI_MODEL = "gemini-3.5-flash"

# Keywords that mean a query changes or removes data, rather than just reading it.
DESTRUCTIVE_KEYWORDS = ("DELETE", "UPDATE", "DROP", "TRUNCATE", "ALTER", "INSERT")


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


@dataclass
class ConversationTurn:
    """One question/SQL pair, kept so follow-up questions have context."""
    question: str
    sql: str


# --------------------------------------------------------------------------- #
# DB connection
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Prompt validation
# --------------------------------------------------------------------------- #

def validate_prompt(prompt: str, min_length: int = MIN_PROMPT_LENGTH) -> PromptValidation:
    cleaned = prompt.strip()
    if len(cleaned) < min_length:
        return PromptValidation(valid=False, reason="Prompt is too short or vague.")
    return PromptValidation(valid=True)


# --------------------------------------------------------------------------- #
# Schema lookup + Gemini SQL generation
# --------------------------------------------------------------------------- #

def get_table_schema(conn, table_name: str) -> List[Tuple[str, str]]:
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT COLUMN_NAME, DATA_TYPE
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
            ORDER BY ORDINAL_POSITION
            """,
            (conn.database, table_name),
        )
        return cursor.fetchall()
    finally:
        cursor.close()


def build_sql_prompt(schema: List[Tuple[str, str]], table_name: str, user_question: str,
                      history: Optional[List[ConversationTurn]] = None) -> str:
    columns_desc = "\n".join(f"  - {name} ({dtype})" for name, dtype in schema)

    history_desc = ""
    if history:
        turns = "\n".join(
            f'  - You asked: "{t.question}" -> generated: {t.sql}' for t in history[-3:]
        )
        history_desc = f"""
Recent conversation, for context on follow-up questions (e.g. "now filter
that to last month" refers back to the most recent query below):
{turns}
"""

    return f"""You are a MySQL expert. Given the table `{table_name}` with these columns:
{columns_desc}
{history_desc}
Write a single valid MySQL query that answers this request:
"{user_question}"

Return ONLY the raw SQL query, with no explanation and no markdown formatting."""


def generate_sql(prompt_text: str, api_key: Optional[str] = None) -> str:
    if genai is None:
        raise RuntimeError(
            "google-generativeai is not installed. Run:\n"
            "  pip install google-generativeai --break-system-packages"
        )
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set.")

    genai.configure(api_key=key)
    model = genai.GenerativeModel(GEMINI_MODEL)
    response = model.generate_content(prompt_text)

    sql = response.text.strip()
    if sql.startswith("```"):
        sql = sql.strip("`")
        if sql.startswith("sql\n"):
            sql = sql[4:]
        sql = sql.strip()
    return sql


def generate_sql_for_question(conn, table_name: str, user_question: str,
                               history: Optional[List[ConversationTurn]] = None,
                               api_key: Optional[str] = None) -> str:
    schema = get_table_schema(conn, table_name)
    if not schema:
        raise ValueError(f"Table '{table_name}' not found or has no columns.")
    prompt_text = build_sql_prompt(schema, table_name, user_question, history=history)
    return generate_sql(prompt_text, api_key=api_key)


def is_destructive_query(sql: str) -> bool:
    """True if the query changes or removes data rather than just reading it."""
    first_word = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
    return first_word in DESTRUCTIVE_KEYWORDS


def explain_sql(sql: str, api_key: Optional[str] = None) -> str:
    """Ask Gemini for a one-or-two sentence, plain-English explanation of a SQL query."""
    prompt_text = f"""Explain what this MySQL query does, in one or two plain-English
sentences a non-technical person could understand. No preamble, no markdown.

{sql}"""
    return generate_sql(prompt_text, api_key=api_key)


def summarize_results(user_question: str, columns: List[str], rows: List[Tuple],
                       api_key: Optional[str] = None) -> str:
    """Ask Gemini for a short plain-English summary of query results."""
    preview_rows = rows[:20]  # keep the prompt small
    rows_desc = "\n".join(", ".join(str(v) for v in row) for row in preview_rows)
    prompt_text = f"""The user asked: "{user_question}"

The query returned these columns: {", ".join(columns)}
And these rows (possibly truncated):
{rows_desc}

Total rows returned: {len(rows)}

Summarize the result in one or two plain-English sentences. No preamble, no markdown."""
    return generate_sql(prompt_text, api_key=api_key)


def diagnose_error(sql: str, error_message: str, api_key: Optional[str] = None) -> str:
    """Ask Gemini to explain a MySQL error in plain English and suggest a fix."""
    prompt_text = f"""This MySQL query failed:
{sql}

With this error:
{error_message}

In two or three plain-English sentences, explain what likely went wrong and
suggest a concrete fix. No preamble, no markdown."""
    return generate_sql(prompt_text, api_key=api_key)


# --------------------------------------------------------------------------- #
# Execute the confirmed query
# --------------------------------------------------------------------------- #

def run_query(conn, sql: str) -> QueryResult:
    cursor = conn.cursor()
    try:
        cursor.execute(sql)
        if cursor.description:  # SELECT
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            return QueryResult(success=True, columns=columns, rows=rows, is_select=True)
        else:  # INSERT/UPDATE/DELETE
            conn.commit()
            return QueryResult(success=True, rowcount=cursor.rowcount, is_select=False)
    except MySQLError as e:
        return QueryResult(success=False, error=str(e))
    finally:
        cursor.close()


# =============================================================== #
# ============================  CLI  =============================== #
# =============================================================== #

def cli_connect_to_database():
    print("--- Database Connection ---")
    host = input("MySQL host (e.g. localhost): ").strip() or "localhost"
    db_user = input("MySQL username: ").strip()
    db_password = input("MySQL password: ")
    database = input("Database name: ").strip()

    result = connect_to_database(host, db_user, db_password, database)
    if result.success:
        print("Correct — connected to the database.\n")
        return result.connection

    print(f"Wrong — could not connect to the database: {result.error}")
    return None


def cli_get_user_prompt() -> str:
    while True:
        prompt = input("\nWhat would you like to ask about your data? ").strip()
        validation = validate_prompt(prompt)

        if validation.valid:
            return prompt

        print(f"Warning: {validation.reason}")
        choice = input("Continue anyway? (y/n): ").strip().lower()
        if choice == "y":
            return prompt
        # otherwise loop back and ask again


_last_sql_run = ""  # used only to give diagnose_error something to reference on failure


def cli_show_query_output(result: QueryResult, user_question: str):
    if not result.success:
        print(f"\nError running query: {result.error}")
        try:
            explanation = diagnose_error(_last_sql_run, result.error)
            print(f"Assistant: {explanation}")
        except RuntimeError:
            pass  # no Gemini key configured — skip the extra explanation
        return

    if result.is_select:
        print("\n--- Output ---")
        print(" | ".join(result.columns))
        print("-" * 40)
        for row in result.rows:
            print(" | ".join(str(v) for v in row))
        print(f"\n({len(result.rows)} row(s) returned)")

        try:
            summary = summarize_results(user_question, result.columns, result.rows)
            print(f"\nAssistant: {summary}")
        except RuntimeError:
            pass
    else:
        print(f"\nQuery executed. {result.rowcount} row(s) affected.")


def cli_main_loop(conn, table_name: str):
    global _last_sql_run
    history: List[ConversationTurn] = []

    while True:
        user_question = cli_get_user_prompt()

        try:
            sql = generate_sql_for_question(conn, table_name, user_question, history=history)
        except (RuntimeError, ValueError) as e:
            print(f"Error: {e}")
            continue

        print("\n--- Generated SQL ---")
        print(sql)

        try:
            explanation = explain_sql(sql)
            print(f"Assistant: {explanation}")
        except RuntimeError:
            pass  # no Gemini key configured — skip the extra explanation

        if is_destructive_query(sql):
            print("⚠️  This query will change or delete data, not just read it. Double-check it before running.")

        run_it = input("\nRun this query? (y/n): ").strip().lower()
        if run_it == "y":
            _last_sql_run = sql
            result = run_query(conn, sql)
            cli_show_query_output(result, user_question)
            history.append(ConversationTurn(question=user_question, sql=sql))
        # "No" loops back to asking for a new prompt (not added to history,
        # since it was never actually run)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def main():
    conn = cli_connect_to_database()
    if conn is None:
        sys.exit("Stopping the program.")

    table_name = input("Which table do you want to query? ").strip()

    try:
        cli_main_loop(conn, table_name)
    except KeyboardInterrupt:
        print("\nExiting.")
    finally:
        if conn.is_connected():
            conn.close()


if __name__ == "__main__":
    main()

