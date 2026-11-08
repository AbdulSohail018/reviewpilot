from bookshelf.inventory import Inventory
from bookshelf.loans import LOAN_DAYS


def stock_report(inv: Inventory) -> str:
    return f"{inv.total_copies()} copies in stock; standard loan is {LOAN_DAYS} days"
