import json
import sqlite3
from pathlib import Path

from bookshelf.models import Book


def connect(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS books (isbn TEXT PRIMARY KEY, title TEXT, author TEXT, price REAL)")
    return conn


def save_book(conn: sqlite3.Connection, book: Book) -> None:
    conn.execute("INSERT OR REPLACE INTO books VALUES (?, ?, ?, ?)", (book.isbn, book.title, book.author, book.price))


def find_by_author(conn: sqlite3.Connection, author: str) -> list[tuple]:
    return conn.execute("SELECT isbn, title FROM books WHERE author = ?", (author,)).fetchall()


def export_json(books: list[Book], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([b.__dict__ for b in books], f)
