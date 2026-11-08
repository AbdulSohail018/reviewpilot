"""A tiny order service used to demonstrate ReviewPilot on a live pull request."""
import sqlite3


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, customer TEXT, total REAL)")
    return conn


def add_order(conn: sqlite3.Connection, customer: str, total: float) -> int:
    cur = conn.execute("INSERT INTO orders (customer, total) VALUES (?, ?)", (customer, total))
    return cur.lastrowid


def orders_for(conn: sqlite3.Connection, customer: str) -> list[tuple]:
    return conn.execute("SELECT id, total FROM orders WHERE customer = ?", (customer,)).fetchall()
