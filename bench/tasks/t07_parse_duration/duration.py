def parse_duration(s):
    total = 0
    num = ""
    for ch in s:
        if ch.isdigit():
            num += ch
        elif ch == "h":
            total += int(num) * 3600
        elif ch == "m":
            total += int(num) * 60
        elif ch == "s":
            total += int(num)
    return total
