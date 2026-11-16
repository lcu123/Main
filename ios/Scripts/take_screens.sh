#!/bin/bash
# Takes pictures of the app's screens in a booted simulator that already has
# the app installed, then tiles them into one contact sheet.
#
#   take_screens.sh <udid> <out-dir> [odd] <entry>...
#
# An entry is a screen name ("today"), or name@N to show customer N of the
# route of awkward names ("confirm@4"). With "odd" every entry uses that route.
#
# The app's screenshot mode (-ZestScreen, simulator builds only) writes a
# small file once a screen is up; this waits for it instead of guessing. A
# picture that is one flat colour is taken again. One missing picture is not
# a failure; no pictures at all is.
set -u

UDID="$1"
OUT="$2"
shift 2
FIXTURE=""
if [ "${1:-}" = "odd" ]; then
  FIXTURE="odd"
  shift
fi

BUNDLE=com.zestlawnpest.video
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$OUT"
WORK="$(mktemp -d)"
DATA=$(xcrun simctl get_app_container "$UDID" "$BUNDLE" data 2>/dev/null || true)
MARKER="$DATA/Documents/zest-screen-ready.txt"

# 0 = the picture is one flat colour (a launch screen, not a real screen).
is_blank() {
  sips -Z 160 "$1" --out "$WORK/check.png" >/dev/null 2>&1 || return 1
  python3 "$HERE/is_blank.py" "$WORK/check.png" >/dev/null 2>&1
}

failed=""
blank=""
slow=""
number=0
for entry in "$@"; do
  number=$((number + 1))
  name="${entry%@*}"
  index=""
  case "$entry" in
    *@*) index="${entry#*@}" ;;
  esac
  label="$name"
  if [ -n "$index" ]; then label="$name-customer-$index"; fi
  file=$(printf "%s/%02d-%s.png" "$OUT" "$number" "$label")

  arguments=(-ZestScreen "$name")
  if [ -n "$FIXTURE" ]; then arguments+=(-ZestFixture "$FIXTURE"); fi
  if [ -n "$index" ]; then arguments+=(-ZestStop "$index"); fi

  ok=1
  if [ -n "$DATA" ]; then rm -f "$MARKER"; fi
  xcrun simctl launch --terminate-running-process "$UDID" "$BUNDLE" "${arguments[@]}" >/dev/null || ok=0

  # Wait (up to 30 s) for the app to say this screen is up.
  ready=0
  if [ "$ok" -eq 1 ] && [ -n "$DATA" ]; then
    for _ in $(seq 1 60); do
      if [ "$(cat "$MARKER" 2>/dev/null)" = "$name" ]; then ready=1; break; fi
      sleep 0.5
    done
  fi
  if [ "$ready" -eq 1 ]; then
    sleep 1
  else
    slow="$slow $label"
    sleep 6
  fi

  if [ "$ok" -eq 1 ]; then
    xcrun simctl io "$UDID" screenshot "$file" >/dev/null 2>&1 || ok=0
  fi
  # A flat picture means the screen had not been drawn yet: try once more.
  if [ "$ok" -eq 1 ] && is_blank "$file"; then
    echo "blank   $label (retaking in 2 s)"
    sleep 2
    xcrun simctl io "$UDID" screenshot "$file" >/dev/null 2>&1 || ok=0
    if [ "$ok" -eq 1 ] && is_blank "$file"; then
      blank="$blank $label"
      echo "---- last log lines from the app for $label ----"
      xcrun simctl spawn "$UDID" log show --last 40s --style compact \
        --predicate 'process == "ZestVideo"' 2>/dev/null | tail -20 || true
    fi
  fi

  if [ "$ok" -eq 1 ] && [ -s "$file" ]; then
    echo "ok      $label"
  else
    echo "FAILED  $label"
    failed="$failed $label"
  fi
done
xcrun simctl terminate "$UDID" "$BUNDLE" >/dev/null 2>&1 || true

if [ -n "$slow" ]; then
  echo "::warning::$(basename "$OUT"): these screens never reported ready (slow, hung or crashed):$slow"
fi
if [ -n "$blank" ]; then
  echo "::warning::$(basename "$OUT"): these pictures are still one flat colour after a retake:$blank"
fi
if [ -n "$failed" ]; then
  echo "::warning::$(basename "$OUT"): these screens could not be captured:$failed"
fi

count=$(find "$OUT" -maxdepth 1 -name '*.png' | wc -l | tr -d ' ')
if [ "$count" -eq 0 ]; then
  echo "::error::No screenshots were captured into $OUT."
  exit 1
fi

# Every screen in one picture. Standard-library Python only.
mkdir -p "$WORK/thumbs"
for picture in "$OUT"/*.png; do
  sips -Z 720 "$picture" --out "$WORK/thumbs/$(basename "$picture")" >/dev/null 2>&1 || true
done
python3 "$HERE/contact_sheet.py" "$WORK/thumbs" "$OUT/00-contact-sheet.png" 5 || echo "contact sheet skipped"
echo "$count pictures in $OUT"
exit 0
