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

The audit also says "Text clipped" about a line with a picture character (an
emoji) in it, because the emoji's font claims more height than it draws. For
such a line the tests measure the air round the drawn text instead: how many
pixels of clear background lie between the ink and the top, the bottom and
the far end of the line's own frame. A line that is cut, or that spills out
of its frame, has none. Those rows are in the same table ("air: ...").

    measure_contrast.py <folder>...         every contrast-readings.tsv under the folders
    measure_contrast.py --png shot.png --rect X,Y,W,H [--scale 3]
                                            one rectangle (in points) of a screenshot
    measure_contrast.py --self-test

It exits 1 if a line the tests let through as "measured" reads under 4.5:1
here, if a line let through as "whole" has ink at an edge here, or if the two
programs disagree. Standard library only.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from contact_sheet import read_png  # noqa: E402

MIN_CONTRAST = 4.5
MIN_SHARE = 0.004
MIN_PIXELS = 12
INK_TOLERANCE = 24
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


def air(rgb, width):
    """How far the drawn text is from each edge of its picture, in pixels.

    The background is the most common colour round the rim of the picture;
    ink is any pixel further than INK_TOLERANCE from it in any colour.
    Returns {"top", "bottom", "left", "right"} or None when nothing is drawn.
    """
    if width <= 0 or len(rgb) < width * 3:
        return None
    height = len(rgb) // 3 // width
    if height <= 0:
        return None
    groups = {}

    def count(x, y):
        at = (y * width + x) * 3
        key = ((rgb[at] >> 3) << 10) | ((rgb[at + 1] >> 3) << 5) | (rgb[at + 2] >> 3)
        groups[key] = groups.get(key, 0) + 1

    for x in range(width):
        count(x, 0)
        count(x, height - 1)
    for y in range(height):
        count(0, y)
        count(width - 1, y)
    background = min(groups, key=lambda key: (-groups[key], key))
    red, green, blue = ((background >> 10) & 31) * 8 + 4, ((background >> 5) & 31) * 8 + 4, (background & 31) * 8 + 4
    top, bottom, left, right = height, -1, width, -1
    index = 0
    for y in range(height):
        for x in range(width):
            apart = max(abs(rgb[index] - red), abs(rgb[index + 1] - green), abs(rgb[index + 2] - blue))
            index += 3
            if apart > INK_TOLERANCE:
                if y < top:
                    top = y
                if y > bottom:
                    bottom = y
                if x < left:
                    left = x
                if x > right:
                    right = x
    if bottom < 0:
        return None
    return {"top": top, "bottom": height - 1 - bottom, "left": left, "right": width - 1 - right}


def air_text(found):
    return "air above %d, below %d, at the end %d px" % (found["top"], found["bottom"], found["right"])


def read_ppm(path, with_width=False):
    """Returns the RGB bytes of a binary PPM (P6, 255), or None.

    With `with_width`, returns (width, bytes).
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    parts = data.split(b"\n", 3)
    if len(parts) < 4 or parts[0] != b"P6" or parts[2] != b"255":
        return None
    try:
        width, height = (int(word) for word in parts[1].split())
    except ValueError:
        return None
    body = parts[3]
    if len(body) < width * height * 3:
        return None
    body = body[:width * height * 3]
    return (width, body) if with_width else body


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
    return (right - left, bytes(out)), "; ".join(notes)


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
    whole_rows = 0
    for path in sorted(files):
        print("lines of text Apple's audit flagged, measured from the pixels (%s)" % os.path.relpath(path))
        print("  %-8s %-22s %-34s %-34s %s" % ("verdict", "screen", "the tests read", "this script reads", "the line"))
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                fields = line.rstrip("\n").split("\t")
                if len(fields) < 7:
                    continue
                verdict, screen, theirs, words, background, name, label = fields[:7]
                if theirs.startswith("air:") or verdict == "whole":
                    # "Text clipped": the air round the drawn text.
                    whole_rows += 1
                    picture = read_ppm(os.path.join(os.path.dirname(path), name), with_width=True) if name else None
                    found = air(picture[1], picture[0]) if picture else None
                    try:
                        top, bottom, left, right = (int(value) for value in theirs[4:].split(","))
                        tests_say = "air above %d, below %d, at the end %d px" % (top, bottom, right)
                    except ValueError:
                        top = bottom = left = right = None
                        tests_say = "(could not be read)"
                    script_says = air_text(found) if found else ("(no picture to read)" if not picture else "(nothing drawn)")
                    problem = ""
                    if verdict == "whole":
                        if found is None:
                            problem = "  <-- let through, but this script cannot read the picture"
                        elif min(found["top"], found["bottom"], found["right"]) < 1:
                            problem = "  <-- let through, but the ink reaches an edge here"
                        elif (found["top"], found["bottom"], found["left"], found["right"]) != (top, bottom, left, right):
                            problem = "  <-- the two readings disagree"
                    if problem:
                        wrong += 1
                    print("  %-8s %-22s %-34s %-34s %s%s" % (verdict, screen[:22], tests_say[:34], script_says[:34], label[:70], problem))
                    continue
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
    if whole_rows:
        print("clipped text: %d line(s) with a picture character measured" % whole_rows)
    print("  measured = Apple's audit flagged it; the pixels read %.1f:1 or better, whole on the screen, nothing over it" % MIN_CONTRAST)
    print("  whole    = flagged as clipped, a line with an emoji in it; clear background above, below and at its end")
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

    # Air round a line: a block of "letters" in a 200 x 40 picture.
    def block(width, height, x0, y0, x1, y1):
        out = bytearray([0xF5]) * (width * height * 3)
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                out[(y * width + x) * 3:(y * width + x) * 3 + 3] = b"\x33\x33\x33"
        return bytes(out)

    check(air(block(530, 108, 2, 10, 517, 99), 530) == {"top": 10, "bottom": 8, "left": 2, "right": 12}, "air round a whole line")
    check(air(block(200, 40, 4, 6, 150, 39), 200)["bottom"] == 0, "a line cut along the bottom has no air there")
    check(air(block(200, 40, 4, 6, 199, 30), 200)["right"] == 0, "a line running off the end has no air there")
    check(air(bytes([0xF5]) * (50 * 20 * 3), 50) is None and air(b"", 0) is None, "nothing drawn: no air")

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
        # A "whole" row: the air the tests read must be what the pixels say.
        ppm("c.ppm", block(200, 40, 4, 6, 150, 30), 200)
        ppm("d.ppm", block(200, 40, 4, 6, 150, 39), 200)
        whole = [
            ["whole", "confirm", "air:6,9,4,49", "", "", "c.ppm", "[clipped text] \"Rivers, Kai\", 100 x 20 at 38,211"],
        ]
        def write(rows):
            with open(os.path.join(folder, READINGS), "w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write("\t".join(row) + "\n")
        write(whole)
        check(table([folder]) == 0, "a whole line read the same by both is right")
        whole[0][5] = "d.ppm"       # in truth the ink reaches the bottom edge
        write(whole)
        check(table([folder]) == 1, "a cut line let through as whole is caught")
        whole[0][5] = "c.ppm"
        whole[0][2] = "air:5,9,4,49"
        write(whole)
        check(table([folder]) == 1, "two air readings that disagree are caught")
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
        cropped, note = crop_png(path, rect, scale)
        if cropped is None:
            print("cannot measure: " + note)
            return 2
        width, pixels = cropped
        reading = read(pixels)
        if reading is None:
            print("one flat colour: no words found")
            return 2
        found = air(pixels, width)
        print("%s  (words are %.1f %% of the rectangle; %s)%s" % (
            describe(reading), reading["share"] * 100, air_text(found) if found else "nothing drawn", ("  NOTE: " + note) if note else ""))
        return 0 if reading["ratio"] >= MIN_CONTRAST else 1
    if not arguments or arguments[0].startswith("-"):
        print(__doc__)
        return 2
    return 1 if table(arguments) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
