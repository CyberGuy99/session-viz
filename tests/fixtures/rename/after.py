def read_csv_rows(path):
    """Read a CSV file into a list of dicts."""
    import csv
    with open(path) as fh:
        reader = csv.DictReader(fh)
        rows = [dict(r) for r in reader]
    return rows


def unrelated():
    return {"a": [1, 2, 3], "b": None}
