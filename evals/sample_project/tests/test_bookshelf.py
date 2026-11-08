from datetime import date

import pytest

from bookshelf.inventory import Inventory
from bookshelf.loans import due_date, late_fee
from bookshelf.models import Book
from bookshelf.pricing import apply_discount, average_price, order_total
from bookshelf.storage import connect, find_by_author, save_book


def book(isbn="1", title="Dune", author="Herbert", price=10.0, copies=1, ratings=None):
    return Book(isbn, title, author, price, copies, ratings or [])


def test_add_merges_copies():
    inv = Inventory()
    inv.add(book(copies=2))
    inv.add(book(copies=3))
    assert inv.get("1").copies == 5


def test_remove_last_copy_deletes():
    inv = Inventory()
    inv.add(book())
    assert inv.remove("1")
    assert inv.get("1") is None


def test_remove_too_many_fails():
    inv = Inventory()
    inv.add(book(copies=1))
    assert not inv.remove("1", copies=2)


def test_search_matches_title_and_author():
    inv = Inventory()
    inv.add(book())
    inv.add(book(isbn="2", title="Emma", author="Austen"))
    assert [b.isbn for b in inv.search("aus")] == ["2"]


def test_top_rated():
    inv = Inventory()
    inv.add(book(isbn="1", ratings=[3, 4]))
    inv.add(book(isbn="2", ratings=[5]))
    inv.add(book(isbn="3"))
    assert [b.isbn for b in inv.top_rated(2)] == ["2", "1"]


def test_total_copies():
    inv = Inventory()
    inv.add(book(copies=2))
    inv.add(book(isbn="2", copies=3))
    assert inv.total_copies() == 5


def test_discount_and_total():
    assert apply_discount(100, 10) == 90
    assert order_total([30, 20], member=True) == 45
    assert order_total([30, 19.99], member=True) == 49.99
    with pytest.raises(ValueError):
        apply_discount(10, 120)


def test_average_price():
    assert average_price([]) == 0.0
    assert average_price([10, 20]) == 15


def test_storage_roundtrip():
    conn = connect()
    save_book(conn, book())
    assert find_by_author(conn, "Herbert") == [("1", "Dune")]


def test_loans():
    start = date(2026, 1, 1)
    assert due_date(start) == date(2026, 1, 15)
    assert late_fee(date(2026, 1, 15), date(2026, 1, 15)) == 0.0
    assert late_fee(date(2026, 1, 15), date(2026, 1, 19)) == 1.0


def test_average_rating_empty():
    assert book().average_rating() == 0.0
