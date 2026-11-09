import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from roman import to_roman
def test_basic(): assert to_roman(3) == "III" and to_roman(8) == "VIII"
def test_sub(): assert to_roman(4) == "IV" and to_roman(9) == "IX" and to_roman(40) == "XL"
def test_big(): assert to_roman(1994) == "MCMXCIV" and to_roman(3999) == "MMMCMXCIX"
