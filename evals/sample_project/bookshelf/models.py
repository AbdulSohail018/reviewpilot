from dataclasses import dataclass, field


@dataclass
class Book:
    isbn: str
    title: str
    author: str
    price: float
    copies: int = 1
    ratings: list[int] = field(default_factory=list)

    def average_rating(self) -> float:
        if not self.ratings:
            return 0.0
        return sum(self.ratings) / len(self.ratings)
