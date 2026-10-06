def parse(s):
    return s.split(",")


def legacy_parse(s):
    out = []
    for part in s.split(";"):
        out.append(part.strip())
    return out
