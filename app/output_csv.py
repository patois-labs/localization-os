"""CSV writers that neutralize spreadsheet formulas at the final output boundary."""

import csv

from app.output_safety import csv_safe_cell

# Readers keep the standard library contract.
reader = csv.reader
DictReader = csv.DictReader
Error = csv.Error
QUOTE_ALL = csv.QUOTE_ALL
QUOTE_MINIMAL = csv.QUOTE_MINIMAL


class _Writer:
    def __init__(self, *args, **kwargs):
        self.raw = csv.writer(*args, **kwargs)

    def writerow(self, row):
        return self.raw.writerow([csv_safe_cell(value) for value in row])

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)


def writer(*args, **kwargs):
    return _Writer(*args, **kwargs)


class DictWriter(csv.DictWriter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        guarded = object.__new__(_Writer)
        guarded.raw = self.writer
        self.writer = guarded
