def merge_intervals(iv):
    out = []
    for s, e in iv:
        if out and s < out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out
