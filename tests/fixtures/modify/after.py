def sig_change(s, sep: str = ",") -> list[str]:
    return s.split(",")


def body_change(s):
    if not s:
        return []
    return [int(x) for x in s.split(",")]


def doc_change(s):
    """Split a comma-separated string into fields."""
    return s.split(",")


def untouched(x):
    return x
