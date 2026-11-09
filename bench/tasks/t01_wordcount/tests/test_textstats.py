import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from textstats import word_count
def test_simple(): assert word_count("a b c") == 3
def test_ws(): assert word_count("a  b\tc\n d") == 4
def test_empty(): assert word_count("") == 0 and word_count("   ") == 0
