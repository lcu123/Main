#!/usr/bin/env python3
"""Tile PNG screenshots into one contact sheet, with a label under each.

Standard library only (no Pillow), so it runs on a bare CI machine:

    python3 contact_sheet.py <folder of small PNGs> <output.png> [columns]

The inputs should already be small (CI shrinks them with `sips -Z 720`): the
PNG decoder here is plain Python and is not fast. It reads 8-bit RGB and RGBA
PNGs without interlacing, which is what `simctl` and `sips` write. An image
it cannot read becomes a grey box, so one bad file never spoils the sheet.
"""

import os
import struct
import sys
import zlib

BACKGROUND = (255, 255, 255)
INK = (0, 0, 0)
FRAME = (110, 32, 185)       # Zest purple
MISSING = (200, 200, 200)
PADDING = 28
LABEL_SCALE = 3
LABEL_GAP = 10

# A 5x7 dot font: just the characters a file name like "03-today" needs.
FONT = {
    "a": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "b": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "c": ["01110", "10001", "10000", "10000", "10000", "10001", "01110"],
    "d": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "e": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "f": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "g": ["01110", "10001", "10000", "10111", "10001", "10001", "01111"],
    "h": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "i": ["01110", "00100", "00100", "00100", "00100", "00100", "01110"],
    "j": ["00111", "00010", "00010", "00010", "00010", "10010", "01100"],
    "k": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
    "l": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "m": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "n": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "o": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "p": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "q": ["01110", "10001", "10001", "10001", "10101", "10010", "01101"],
    "r": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "s": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "t": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "u": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "v": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "w": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "x": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["01110", "10000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    " ": ["00000", "00000", "00000", "00000", "00000", "00000", "00000"],
}
LABEL_HEIGHT = 7 * LABEL_SCALE


def read_png(path):
    """Return (width, height, rows) with each row a bytes of RGB, or None."""
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    position = 8
    header = None
    compressed = bytearray()
    while position + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[position:position + 8])
        body = data[position + 8:position + 8 + length]
        position += 12 + length
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            compressed += body
        elif kind == b"IEND":
            break
    if header is None:
        return None
    width, height, depth, color, _, _, interlace = header
    if depth != 8 or interlace != 0 or color not in (2, 6):
        return None
    channels = 3 if color == 2 else 4
    stride = width * channels
    raw = zlib.decompress(bytes(compressed))
    if len(raw) < (stride + 1) * height:
        return None

    rows = []
    previous = bytearray(stride)
    offset = 0
    for _ in range(height):
        kind = raw[offset]
        line = bytearray(raw[offset + 1:offset + 1 + stride])
        offset += stride + 1
        if kind == 1:        # Sub
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif kind == 2:      # Up
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif kind == 3:      # Average
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:      # Paeth
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up = previous[i]
                corner = previous[i - channels] if i >= channels else 0
                estimate = left + up - corner
                d_left, d_up, d_corner = abs(estimate - left), abs(estimate - up), abs(estimate - corner)
                if d_left <= d_up and d_left <= d_corner:
                    best = left
                elif d_up <= d_corner:
                    best = up
                else:
                    best = corner
                line[i] = (line[i] + best) & 0xFF
        elif kind != 0:
            return None
        previous = line
        if channels == 4:
            # Flatten onto white and drop the alpha channel.
            rgb = bytearray(width * 3)
            for x in range(width):
                alpha = line[x * 4 + 3]
                for c in range(3):
                    rgb[x * 3 + c] = (line[x * 4 + c] * alpha + 255 * (255 - alpha)) // 255
            rows.append(bytes(rgb))
        else:
            rows.append(bytes(line))
    return width, height, rows


def write_png(path, width, height, rows):
    raw = b"".join(b"\x00" + bytes(row) for row in rows)

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    with open(path, "wb") as handle:
        handle.write(png)


def text_width(text):
    return max(0, len(text) * 6 * LABEL_SCALE - LABEL_SCALE)


def draw_text(canvas, x, y, text):
    for index, character in enumerate(text.lower()):
        glyph = FONT.get(character, FONT["-"])
        left = x + index * 6 * LABEL_SCALE
        for row, bits in enumerate(glyph):
            for column, bit in enumerate(bits):
                if bit != "1":
                    continue
                for dy in range(LABEL_SCALE):
                    line = canvas[y + row * LABEL_SCALE + dy]
                    start = (left + column * LABEL_SCALE) * 3
                    line[start:start + 3 * LABEL_SCALE] = bytes(INK) * LABEL_SCALE


def fill(canvas, x, y, width, height, color):
    for row in range(y, y + height):
        canvas[row][x * 3:(x + width) * 3] = bytes(color) * width


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    folder, output = sys.argv[1], sys.argv[2]
    names = sorted(name for name in os.listdir(folder) if name.lower().endswith(".png"))
    if not names:
        sys.exit("No PNG files in %s" % folder)
    columns = int(sys.argv[3]) if len(sys.argv) > 3 else min(5, len(names))

    images = []
    for name in names:
        try:
            image = read_png(os.path.join(folder, name))
        except Exception as error:      # one bad file must not spoil the sheet
            print("could not read %s: %s" % (name, error))
            image = None
        if image is None:
            print("skipped %s (not an 8-bit RGB/RGBA PNG)" % name)
        images.append((os.path.splitext(name)[0], image))

    readable = [image for _, image in images if image is not None]
    cell_width = max([image[0] for image in readable] + [text_width(label) for label, _ in images] + [120])
    cell_height = max([image[1] for image in readable] + [200])
    rows_count = (len(images) + columns - 1) // columns
    block_height = cell_height + LABEL_GAP + LABEL_HEIGHT

    sheet_width = PADDING + columns * (cell_width + PADDING)
    sheet_height = PADDING + rows_count * (block_height + PADDING)
    canvas = [bytearray(bytes(BACKGROUND) * sheet_width) for _ in range(sheet_height)]

    for index, (label, image) in enumerate(images):
        column, row = index % columns, index // columns
        left = PADDING + column * (cell_width + PADDING)
        top = PADDING + row * (block_height + PADDING)
        if image is None:
            fill(canvas, left, top, cell_width, cell_height, MISSING)
        else:
            width, height, pixels = image
            x = left + (cell_width - width) // 2
            # A thin purple frame, so a white screen still has an edge.
            fill(canvas, x - 2, top - 2, width + 4, height + 4, FRAME)
            for y in range(height):
                canvas[top + y][x * 3:(x + width) * 3] = pixels[y]
        draw_text(canvas, left + (cell_width - text_width(label)) // 2, top + cell_height + LABEL_GAP, label)

    write_png(output, sheet_width, sheet_height, canvas)
    print("wrote %s (%dx%d, %d screens)" % (output, sheet_width, sheet_height, len(images)))


if __name__ == "__main__":
    main()
