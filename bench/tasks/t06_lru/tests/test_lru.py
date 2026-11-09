import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lru import LRUCache
def test_basic():
    c = LRUCache(2); c.put(1, 1); c.put(2, 2); assert c.get(1) == 1
    c.put(3, 3); assert c.get(2) == -1 and c.get(3) == 3
def test_update_no_evict():
    c = LRUCache(2); c.put(1, 1); c.put(2, 2); c.put(1, 10)
    assert c.get(1) == 10 and c.get(2) == 2
