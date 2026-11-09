def flatten(x):
    out = []
    for item in x:
        if isinstance(item, list):
            out.extend(item)
        else:
            out.append(item)
    return out
