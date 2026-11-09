import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from nested import flatten
def test_deep(): assert flatten([1, [2, [3, [4]]]]) == [1, 2, 3, 4]
def test_tuple(): assert flatten([(1, 2), [3, (4,)]]) == [1, 2, 3, 4]
def test_strings(): assert flatten(["ab", ["cd", b"ef"]]) == ["ab", "cd", b"ef"]
