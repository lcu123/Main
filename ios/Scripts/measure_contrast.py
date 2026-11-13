#!/usr/bin/env python3
"""Measures the contrast of words from the pixels of a picture.

Apple's accessibility audit sometimes says "Contrast failed" about a line of
text whose two colours, by arithmetic, are far apart. This settles it by
looking: the most common colour in a picture of the line is what the words
sit on; the colour that stands out most from it (among colours that fill a
real share of the picture) is the words. The soft edge of each letter is a
blend of the two, so it always stands out less and never wins.

The UI tests do the same sum in Swift (`ZestCore/PixelContrast.swift`) on
every line the audit flags, save the pixels they measured, and write one row
per line to `contrast-readings.tsv`. This script reads those same pixels again
and prints the table, so the numbers are in the job log every time and do not
rest on one program's word.

    measure_contrast.py <folder>...         every contrast-readings.tsv under the folders
    measure_contrast.py --png shot.png --rect X,Y,W,H [--scale 3]
                                            one rectangle (in points) of a screenshot
    measure_contrast.py --self-test

It exits 1 if a line the tests let through as "measured" reads under 4.5:1
here, or if the two programs disagree. Standard library only.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from contact_sheet import read_png  # noqa: E402

MIN_CONTRAST = 4.5
MIN_SHARE = 0.004
MIN_PIXELS = 12
READINGS = "contrast-readings.tsv"


def luminance(colour):
    """How bright a colour is to the eye, 0 to 1 (the WCAG relative luminance)."""
    def channel(value):
        value = value / 255.0
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(colour[0]) + 0.7152 * channel(colour[1]) + 0.0722 * channel(colour[2])


def contrast(first, second):
    a, b = luminance(first), luminance(second)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


def hex_text(colour):
    return "#%02X%02X%02X" % tuple(colour)


def one_decimal(value):
    """One decimal place, rounded down: a 4.49 is never shown as 4.5."""
    tenths = int(value * 10)
    return "%d.%d" % (tenths // 10, tenths % 10)


def read(rgb):
    """Reads a picture of one line of text. `rgb` is bytes: r, g, b per pixel.

    Returns {"background", "words", "ratio", "share"} or None when there is
    nothing to read (no pixels, or one flat colour).
    """
    count = len(rgb) // 3
    if count == 0:
        return None
    groups = {}
    for index in range(0, count * 3, 3):
        r, g, b = rgb[index], rgb[index + 1], rgb[index + 2]
        key = ((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3)
        entry = groups.get(key)
        if entry is None:
            groups[key] = [1, r, g, b]
        else:
            entry[0] += 1
            entry[1] += r
            entry[2] += g
            entry[3] += b

    def colour(entry):
        n = entry[0]
        return tuple((entry[i] * 2 + n) // (2 * n) for i in (1, 2, 3))

    # The most common colour; of two equally common, the lower group number
    # (as the Swift does, which walks the groups in order).
    background_key = min(groups, key=lambda key: (-groups[key][0], key))
    background = colour(groups[background_key])
    enough = max(MIN_PIXELS, -(-count * 4 // 1000))   # 0.4 % of the pixels, rounded up
    best = None
    for key in sorted(groups):
        entry = groups[key]
        if key == background_key or entry[0] < enough:
            continue
        ratio = contrast(colour(entry), background)
        if best is None or ratio > best[1]:
            best = (entry, ratio)
    if best is None:
        return None
    return {
        "background": background,
        "words": colour(best[0]),
        "ratio": best[1],
        "share": best[0][0] / float(count),
    }


def read_ppm(path):
    """Returns the RGB bytes of a binary PPM (P6, 255), or None."""
    with open(path, "rb") as handle:
        data = handle.read()
    parts = data.split(b"\n", 3)
    if len(parts) < 4 or parts[0] != b"P6" or parts[2] != b"255":
        return None
    try:
        width, height = (int(word) for word in parts[1].split())
    except ValueError:
        return None
    body = parts[3]
    return body[:width * height * 3] if len(body) >= width * height * 3 else None


def crop_png(path, rect, scale):
    """The RGB bytes of one rectangle (x, y, w, h in points) of a PNG."""
    picture = read_png(path)
    if picture is None:
        return None, "not a PNG this script can read"
    width, height, rows = picture
    x, y, w, h = (int(round(value * scale)) for value in rect)
    notes = []
    if x < 0 or y < 0 or x + w > width or y + h > height:
        notes.append("the rectangle runs off the picture (%d x %d px): only the part inside it is measured" % (width, height))
    left, top = max(0, x), max(0, y)
    right, bottom = min(width, x + w), min(height, y + h)
    if right <= left or bottom <= top:
        return None, "the rectangle is outside the picture"
    out = bytearray()
    for row in rows[top:bottom]:
        out += row[left * 3:right * 3]
    return bytes(out), "; ".join(notes)


def describe(reading):
    return "%s:1, words %s on %s" % (one_decimal(reading["ratio"]), hex_text(reading["words"]), hex_text(reading["background"]))


def table(folders):
    """Prints every reading under the folders. Returns the number of rows that are wrong."""
    files = []
    for folder in folders:
        for root, _, names in os.walk(folder):
            if READINGS in names:
                files.append(os.path.join(root, READINGS))
    if not files:
        print("contrast: no line of text was flagged by Apple's audit and measured (no %s under %s)" % (READINGS, ", ".join(folders)))
        return 0
    wrong = 0
    rows = 0
    for path in sorted(files):
        print("contrast, measured from the pixels (%s)" % os.path.relpath(path))
        print("  %-8s %-22s %-34s %-34s %s" % ("verdict", "screen", "the tests read", "this script reads", "the line"))
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                fields = line.rstrip("\n").split("\t")
                if len(fields) < 7:
                    continue
                verdict, screen, theirs, words, background, name, label = fields[:7]
                rows += 1
                pixels = read_ppm(os.path.join(os.path.dirname(path), name)) if name else None
                mine = read(pixels) if pixels else None
                tests_say = "%s:1 %s on %s" % (theirs, words, background) if theirs else "(could not be read)"
                if mine is None:
                    script_says = "(no picture to read)" if not pixels else "(one flat colour: no words found)"
                else:
                    script_says = describe(mine)
                problem = ""
                if verdict == "measured":
                    # The tests let this one through on their own reading.
                    if mine is None:
                        problem = "  <-- let through, but this script cannot read the picture"
                    elif mine["ratio"] < MIN_CONTRAST:
                        problem = "  <-- let through, but reads UNDER %.1f:1 here" % MIN_CONTRAST
                    else:
                        try:
                            if abs(float(theirs) - mine["ratio"]) > 0.25:
                                problem = "  <-- the two readings disagree"
                        except ValueError:
                            problem = "  <-- the tests' number is not a number"
                if problem:
                    wrong += 1
                print("  %-8s %-22s %-34s %-34s %s%s" % (verdict, screen[:22], tests_say[:34], script_says[:34], label[:70], problem))
    print("contrast: %d line(s) measured, %d wrong" % (rows, wrong))
    print("  measured = Apple's audit flagged it; the pixels read %.1f:1 or better, whole on the screen, nothing over it" % MIN_CONTRAST)
    print("  FAILED   = Apple's audit flagged it and the pixels do not clear it (too faint, cut off, or covered)")
    return wrong


def self_test():
    def picture(words, background, word_share=0.18, edge_share=0.08, pixels=6000):
        out = bytearray()
        solid, soft = int(pixels * word_share), int(pixels * edge_share)
        for index in range(pixels):
            mix = 0.0
            if index < solid:
                mix = 1.0
            elif index < solid + soft:
                mix = (index - solid + 1) / float(soft + 1)
            for channel in range(3):
                out.append(int(round(background[channel] + (words[channel] - background[channel]) * mix)))
        return bytes(out)

    def check(condition, what):
        if not condition:
            print("FAILED: " + what)
            sys.exit(1)

    ink, page, blue, white, grey, cyan = (0x33,) * 3, (0xF5,) * 3, (0x1A, 0x68, 0xC4), (0xFF,) * 3, (0x8A,) * 3, (0xE0, 0xF7, 0xFA)
    reading = read(picture(ink, page))
    check(reading["words"] == ink and reading["background"] == page, "ink on the page: the two colours")
    check(abs(reading["ratio"] - contrast(ink, page)) < 0.01 and 11.5 < reading["ratio"] < 11.7, "ink on the page is 11.6:1")
    check(abs(reading["share"] - 0.18) < 0.01, "the share of the picture that is words")
    reading = read(picture(white, (0x1A, 0x64, 0xB9)))
    check(reading["words"] == white, "white words on a dark button are read the same way")
    reading = read(picture(blue, white, word_share=0.10, edge_share=0.33))
    check(reading["words"] == blue, "soft edges never count as the words")
    check(read(picture(grey, cyan))["ratio"] < 4.5, "faint words are caught")
    speck = bytearray(picture((0x77,) * 3, white))
    speck[-30:] = bytes(30)
    reading = read(bytes(speck))
    check(reading["words"] == (0x77,) * 3 and reading["ratio"] < 4.5, "ten stray dark pixels are not the words")
    check(read(b"") is None and read(bytes([0xF5]) * 3000) is None, "nothing to read: no reading")
    check(one_decimal(4.49) == "4.4" and one_decimal(4.5) == "4.5" and one_decimal(11.593) == "11.5", "one decimal, rounded down")
    check(hex_text((0x1A, 0x68, 0xC4)) == "#1A68C4", "hex text")

    # The table: a folder with two readings, one let through rightly and one wrongly.
    with tempfile.TemporaryDirectory() as folder:
        def ppm(name, rgb, width):
            with open(os.path.join(folder, name), "wb") as handle:
                handle.write(b"P6\n%d %d\n255\n" % (width, len(rgb) // 3 // width) + rgb)
        ppm("a.ppm", picture(ink, page), 100)
        ppm("b.ppm", picture(grey, cyan), 100)
        check(read_ppm(os.path.join(folder, "a.ppm")) == picture(ink, page), "a PPM is read back byte for byte")
        rows = [
            ["measured", "login", "11.59", "#333333", "#F5F5F5", "a.ppm", "\"Type your mobile number.\", 397 x 41 at 18,225"],
            ["FAILED", "queue", "2.63", "#8A8A8A", "#E0F7FA", "b.ppm", "\"faint\", 100 x 30 at 10,10"],
        ]
        with open(os.path.join(folder, READINGS), "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write("\t".join(row) + "\n")
        check(table([folder]) == 0, "a right table has nothing wrong in it")
        rows[1][0] = "measured"     # the tests "let through" the faint one
        with open(os.path.join(folder, READINGS), "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write("\t".join(row) + "\n")
        check(table([folder]) == 1, "a faint line let through is caught")
        rows[1][0] = "FAILED"
        rows[0][2] = "7.00"         # the tests' number is not what the pixels say
        with open(os.path.join(folder, READINGS), "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write("\t".join(row) + "\n")
        check(table([folder]) == 1, "two readings that disagree are caught")
        check(table([os.path.join(folder, "nothing-here")]) == 0, "no readings is not an error")
    print("self-test passed")


def main(arguments):
    if arguments == ["--self-test"]:
        self_test()
        return 0
    if arguments and arguments[0] == "--png":
        try:
            path = arguments[1]
            rect = [float(value) for value in arguments[arguments.index("--rect") + 1].split(",")]
            scale = float(arguments[arguments.index("--scale") + 1]) if "--scale" in arguments else 3.0
            if len(rect) != 4:
                raise ValueError
        except (IndexError, ValueError):
            print(__doc__)
            return 2
        pixels, note = crop_png(path, rect, scale)
        if pixels is None:
            print("cannot measure: " + note)
            return 2
        reading = read(pixels)
        if reading is None:
            print("one flat colour: no words found")
            return 2
        print("%s  (words are %.1f %% of the rectangle)%s" % (describe(reading), reading["share"] * 100, ("  NOTE: " + note) if note else ""))
        return 0 if reading["ratio"] >= MIN_CONTRAST else 1
    if not arguments or arguments[0].startswith("-"):
        print(__doc__)
        return 2
    return 1 if table(arguments) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
