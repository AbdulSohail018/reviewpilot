from bookshelf.models import Book


class Inventory:
    def __init__(self):
        self._books: dict[str, Book] = {}

    def add(self, book: Book) -> None:
        if book.isbn in self._books:
            self._books[book.isbn].copies += book.copies
        else:
            self._books[book.isbn] = book

    def get(self, isbn: str) -> Book | None:
        return self._books.get(isbn)

    def remove(self, isbn: str, copies: int = 1) -> bool:
        book = self._books.get(isbn)
        if book is None or book.copies < copies:
            return False
        book.copies -= copies
        if book.copies == 0:
            del self._books[isbn]
        return True

    def search(self, text: str) -> list[Book]:
        text = text.lower()
        return [b for b in self._books.values() if text in b.title.lower() or text in b.author.lower()]

    def top_rated(self, n: int = 3) -> list[Book]:
        rated = [b for b in self._books.values() if b.ratings]
        return sorted(rated, key=lambda b: b.average_rating(), reverse=True)[:n]

    def total_copies(self) -> int:
        return sum(b.copies for b in self._books.values())
