#!/usr/bin/env python3
"""Is this screenshot one flat colour (a launch screen, not a real screen)?

    python3 is_blank.py <small.png>

Exit code 0 = blank, 1 = it shows something, 2 = could not tell.

Give it a SMALL copy of the screenshot (CI makes one with `sips -Z 160`):
the PNG reader is plain Python. The top eighth, where the clock and battery
sit, is ignored: a blank screen still has a status bar.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from contact_sheet import read_png  # noqa: E402

# How far apart two shades may be and still count as "the same colour".
# Shrinking the picture and colour management nudge values by a step or two.
TOLERANCE = 8


def is_flat(rows, width):
    start = len(rows) // 8
    lows = [255, 255, 255]
    highs = [0, 0, 0]
    for row in rows[start:]:
        for channel in range(3):
            values = row[channel::3]
            lows[channel] = min(lows[channel], min(values))
            highs[channel] = max(highs[channel], max(values))
    return all(high - low <= TOLERANCE for low, high in zip(lows, highs))


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    try:
        image = read_png(sys.argv[1])
    except Exception as error:
        print("could not read %s: %s" % (sys.argv[1], error))
        return 2
    if image is None:
        print("could not read %s" % sys.argv[1])
        return 2
    width, _, rows = image
    return 0 if is_flat(rows, width) else 1


if __name__ == "__main__":
    sys.exit(main())
