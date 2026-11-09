import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from duration import parse_duration
def test_hm(): assert parse_duration("1h30m") == 5400
def test_all(): assert parse_duration("1h2m3s") == 3723
def test_s(): assert parse_duration("45s") == 45
@pytest.mark.parametrize("bad", ["", "5x", "h1", "10"])
def test_bad(bad):
    with pytest.raises(ValueError): parse_duration(bad)
