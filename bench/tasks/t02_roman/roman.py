VALUES = [(1000, "M"), (500, "D"), (100, "C"), (50, "L"), (10, "X"), (5, "V"), (1, "I")]

def to_roman(n):
    out = ""
    for v, s in VALUES:
        while n >= v:
            out += s
            n -= v
    return out
