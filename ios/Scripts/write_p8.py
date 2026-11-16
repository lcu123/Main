#!/usr/bin/env python3
"""Write the App Store Connect key file from the secret ASC_KEY_P8, whatever shape
the secret was pasted in.

Run by the testflight job in .github/workflows/ios.yml. A .p8 file is three parts:
a first line, the key in base64, a last line. Pasted into a web form it often
arrives as one line, or with "\\n" typed out, or with only the middle part. This
takes the key's own characters, puts the line breaks back and writes a proper
file to the path in KEY.

It prints the SHAPE of what it was given (how long, how many lines, which parts
are there) so a wrong secret can be told from a mangled one. It never prints a
character of the key. Exit 1 with a plain reason when no key can be made of it.
"""
import base64
import binascii
import os
import re
import sys


def stop(text):
    print("::error::" + text.replace("\n", " "), flush=True)
    sys.exit(1)


def rebuild(raw):
    """Returns (pem text or None, shape as a string, the reason when None)."""
    text = raw.replace("\r", "")
    typed_breaks = "\\n" in text
    shape = "%d characters, %d line(s), first line %s, last line %s, line breaks typed out as \\n: %s" % (
        len(text), len([line for line in text.split("\n") if line.strip()]),
        "there" if "-----BEGIN" in text else "MISSING",
        "there" if "-----END" in text else "MISSING",
        "yes" if typed_breaks else "no")
    if typed_breaks:
        text = text.replace("\\n", "\n")
    kind = "PRIVATE KEY"
    found = re.search(r"-----BEGIN ((?:EC |ENCRYPTED )?PRIVATE KEY)-----(.*?)-----END \1-----", text, re.S)
    if found:
        kind, middle = found.group(1), found.group(2)
    else:
        # No complete first and last line: take what lies between whatever is there.
        middle = re.sub(r"-----(BEGIN|END)[A-Z ]*-----", " ", text)
    middle = re.sub(r"\s+", "", middle)
    shape += ", key part %d characters" % len(middle)
    if not middle:
        return None, shape, "the secret is empty between its first and last line"
    if re.search(r"[^A-Za-z0-9+/=]", middle):
        return None, shape, "there are characters in it that no key file has"
    try:
        decoded = base64.b64decode(middle + "=" * (-len(middle) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None, shape, "the key part is not whole (it was cut off, or something was typed into it)"
    shape += " = %d bytes" % len(decoded)
    if len(decoded) < 100:
        if len(middle) < 60:
            return None, shape, ("that is far too short for a key file; it looks like the Key ID or the Issuer ID "
                                 "was saved here instead of the file's contents")
        return None, shape, "it is too short to be a whole key file: part of it was cut off"
    middle = middle.rstrip("=") + "=" * (-len(middle.rstrip("=")) % 4)
    lines = [middle[at:at + 64] for at in range(0, len(middle), 64)]
    return "-----BEGIN %s-----\n%s\n-----END %s-----\n" % (kind, "\n".join(lines), kind), shape, ""


def main():
    pem, shape, reason = rebuild(os.environ.get("ASC_KEY_P8", ""))
    print("Shape of the secret ASC_KEY_P8 (nothing of the key itself): " + shape, flush=True)
    if pem is None:
        stop("Repository secret ASC_KEY_P8 cannot be made into a key file: %s. Open the downloaded "
             "AuthKey_XXXX.p8 file in a text editor, copy ALL of it, and save it as the secret again." % reason)
    with open(os.environ["KEY"], "w") as handle:
        handle.write(pem)


if __name__ == "__main__":
    main()
