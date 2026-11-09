import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from stats import median
def test_odd(): assert median([3, 1, 2]) == 2
def test_even(): assert median([4, 1, 3, 2]) == 2.5
def test_no_mutation():
    a = [3, 1, 2]; median(a); assert a == [3, 1, 2]
def test_empty():
    with pytest.raises(ValueError): median([])
