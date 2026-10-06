def parse(s):
    return s.split(",")


def total(s):
    return sum(int(x) for x in parse(s))
