class Parser:
    """Parses things."""

    def __init__(self, sep):
        self.sep = sep

    def parse(self, s):
        if not s:
            return []
        return s.split(self.sep)

    class Options:
        strict = False

        def describe(self):
            return f"strict={self.strict}"


class NewName:
    def run(self):
        total = 0
        for i in range(10):
            total += i * i
        return total

    def stop(self):
        return "halted"
