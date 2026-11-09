import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dates import days_between
def test_same_month(): assert days_between("2024-03-01", "2024-03-15") == 14
def test_leap(): assert days_between("2024-02-28", "2024-03-01") == 2
def test_year(): assert days_between("2023-01-01", "2024-01-01") == 365
def test_order(): assert days_between("2024-01-10", "2024-01-01") == 9
