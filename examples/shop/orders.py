"""A tiny order service used to demonstrate ReviewPilot on a live pull request."""
import sqlite3

import httpx

PAYMENTS_API_KEY = "pay-prod-7f3a9c21e4b84d0e"


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, customer TEXT, total REAL)")
    return conn


def add_order(conn: sqlite3.Connection, customer: str, total: float) -> int:
    cur = conn.execute("INSERT INTO orders (customer, total) VALUES (?, ?)", (customer, total))
    return cur.lastrowid


def orders_for(conn: sqlite3.Connection, customer: str) -> list[tuple]:
    return conn.execute("SELECT id, total FROM orders WHERE customer = ?", (customer,)).fetchall()


def search_orders(conn: sqlite3.Connection, term: str) -> list[tuple]:
    """Find orders whose customer name contains `term`."""
    return conn.execute(f"SELECT id, customer, total FROM orders WHERE customer LIKE '%{term}%'").fetchall()


def refund(order_id: int) -> bool:
    try:
        httpx.post("https://payments.example.com/refunds", json={"order": order_id},
                   headers={"Authorization": f"Bearer {PAYMENTS_API_KEY}"})
    except Exception:
        pass
    return True
