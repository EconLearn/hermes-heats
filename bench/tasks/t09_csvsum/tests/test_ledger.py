import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ledger import total_by_category
def test_simple():
    assert total_by_category("category,amount\nfood,10\nfood,2.5\nrent,100") == {"food": 12.5, "rent": 100.0}
def test_quoted():
    t = 'category,amount\nrent,"1,200.50"\nfood,0.1\nfood,0.2'
    assert total_by_category(t) == {"rent": 1200.5, "food": 0.3}
