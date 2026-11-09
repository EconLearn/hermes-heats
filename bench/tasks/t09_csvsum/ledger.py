def total_by_category(text):
    totals = {}
    for line in text.strip().splitlines()[1:]:
        cat, amount = line.split(",")
        totals[cat] = totals.get(cat, 0) + float(amount)
    return totals
