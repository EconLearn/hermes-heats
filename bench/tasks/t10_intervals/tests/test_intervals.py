import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from intervals import merge_intervals
def test_basic(): assert merge_intervals([[1, 3], [2, 6], [8, 10]]) == [[1, 6], [8, 10]]
def test_unsorted(): assert merge_intervals([[8, 10], [1, 3], [2, 6]]) == [[1, 6], [8, 10]]
def test_touch(): assert merge_intervals([[1, 2], [2, 3]]) == [[1, 3]]
def test_no_mutation():
    a = [[2, 6], [1, 3]]; merge_intervals(a); assert a == [[2, 6], [1, 3]]
