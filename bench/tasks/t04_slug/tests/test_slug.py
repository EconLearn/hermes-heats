import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from slug import slugify
def test_basic(): assert slugify("Hello World") == "hello-world"
def test_punct(): assert slugify("Hello, World!!") == "hello-world"
def test_runs(): assert slugify("  a -- b  ") == "a-b"
def test_digits(): assert slugify("Top 10 Tips") == "top-10-tips"
