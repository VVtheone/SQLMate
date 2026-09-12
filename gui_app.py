"""
Simple Tkinter GUI:
- A text entry box that stores its content into a string variable `user_input`.
- A scrollable table (Treeview) meant to display SQL query results.
  It's empty by default, but includes a `load_table_data(columns, rows)`
  helper function you can call to populate it (e.g. after running a
  SQL query with sqlite3, pyodbc, psycopg2, etc.).

Run standalone with:
    python gui_app.py

Or import and call launch_app() from another script (e.g. main.py).
"""

import tkinter as tk
from tkinter import ttk


def launch_app():
    # This will hold whatever the user types, as a plain string.
    user_input = ""

    def submit_input():
        """Reads the entry box, stores it in the (function-local) user_input variable."""
        nonlocal user_input
        user_input = entry_var.get()
        print(f"user_input set to: {user_input!r}")
        status_label.config(text=f"Stored: {user_input}")

    def load_table_data(columns, rows):
        """
        Populate the table with data (e.g. results from an SQL query).

        columns: list/tuple of column names, e.g. ["id", "name", "email"]
        rows:    list of tuples/lists, one per row, e.g. [(1, "Alice", "a@x.com"), ...]

        Example after running a query with sqlite3:
            cursor.execute("SELECT id, name, email FROM users")
            col_names = [desc[0] for desc in cursor.description]
            data = cursor.fetchall()
            load_table_data(col_names, data)
        """
        tree.delete(*tree.get_children())
        tree["columns"] = columns
        tree["show"] = "headings"

        for col in columns:
            tree.heading(col, text=col)
            tree.column(col, width=120, anchor="w")

        for row in rows:
            tree.insert("", "end", values=row)

    # ---------------- Main window ----------------
    root1 = tk.Tk()
    root1.title("Text Input + SQL Table Viewer")
    root1.geometry("700x450")

    # ---- Input section ----
    input_frame = ttk.Frame(root1, padding=10)
    input_frame.pack(fill="x")

    ttk.Label(input_frame, text="Enter text:").pack(side="left", padx=(0, 5))

    entry_var = tk.StringVar()
    entry = ttk.Entry(input_frame, textvariable=entry_var, width=50)
    entry.pack(side="left", fill="x", expand=True, padx=(0, 5))
    entry.bind("<Return>", lambda event: submit_input())

    submit_btn = ttk.Button(input_frame, text="Submit", command=submit_input)
    submit_btn.pack(side="left")

    status_label = ttk.Label(root1, text="Stored: (nothing yet)", padding=(10, 0))
    status_label.pack(fill="x")

    # ---- Scrollable table section ----
    table_frame = ttk.Frame(root1, padding=10)
    table_frame.pack(fill="both", expand=True)

    tree_scroll_y = ttk.Scrollbar(table_frame, orient="vertical")
    tree_scroll_x = ttk.Scrollbar(table_frame, orient="horizontal")

    tree = ttk.Treeview(
        table_frame,
        yscrollcommand=tree_scroll_y.set,
        xscrollcommand=tree_scroll_x.set,
    )

    tree_scroll_y.config(command=tree.yview)
    tree_scroll_x.config(command=tree.xview)

    tree_scroll_y.pack(side="right", fill="y")
    tree_scroll_x.pack(side="bottom", fill="x")
    tree.pack(fill="both", expand=True)

    # Table starts empty - no columns/rows set yet.
    # Call load_table_data(columns, rows) to fill it in later.

    # mainloop() runs unconditionally here -- this is what actually
    # keeps the window open, whether launch_app() is called directly
    # (python gui_app.py) or imported and called from another script.
    root1.mainloop()


if __name__ == "__main__":
    launch_app()
