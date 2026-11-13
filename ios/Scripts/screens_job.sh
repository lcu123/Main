#!/bin/bash
# What one "screen sizes" CI job does: boot the right simulators, set the text
# size, take the pictures, and run the layout and accessibility checks
# (ScreensUITests) on each. Run from the ios/ folder, after the app and the UI
# tests have been built into build/DerivedData.
#
#   screens_job.sh small        the smallest iPhone: usual route, then the awkward names
#   screens_job.sh mid-ipad     a 6.1-inch iPhone, then an iPad
#   screens_job.sh large-text   the big iPhone: ordinary text, the largest the app
#                               allows, and the largest accessibility size
#
# Pictures go to out/<artifact name>/, logs and reports to ui-artifacts/.
# Every pass is run even if an earlier one failed; the exit code says whether
# any did.
set -u

VARIANT="${1:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."

APP="build/DerivedData/Build/Products/Debug-iphonesimulator/ZestVideo.app"
SUITE="ZestVideoUITests/ScreensUITests"
ODD_TEST="$SUITE/testOddNamesAreNeverCutOff"
ALL="login code today today2 confirm far record recording review warning saved stopvideo locked queue update"
# The awkward names: the lists, then the screen with the largest type for
# every customer, then the other screens for the four longest names.
ODD="today today2 queue queue@4 queue@8"
ODD="$ODD confirm confirm@1 confirm@2 confirm@3 confirm@4 confirm@5 confirm@6 confirm@7 confirm@8 confirm@9"
for index in 2 4 5 9; do
  ODD="$ODD record@$index review@$index warning@$index stopvideo@$index locked@$index"
done

mkdir -p ui-artifacts/failures out
FAILED_PASSES=""

if [ ! -d "$APP" ]; then
  echo "::error::The app has not been built ($APP is missing)."
  exit 1
fi
# The build is unsigned; an ad-hoc signature costs nothing and avoids a
# simulator that refuses to launch it.
codesign --force --sign - --timestamp=none "$APP" 2>/dev/null || echo "ad-hoc signing skipped"

pick() {
  python3 Scripts/pick_simulator.py "$1"
}

boot() {
  local udid="$1"
  xcrun simctl boot "$udid" 2>/dev/null || true
  xcrun simctl bootstatus "$udid" -b
  # A clean status bar: 9:41, full battery, full signal. Nice to have only.
  xcrun simctl status_bar "$udid" override --time "9:41" --batteryState charged \
    --batteryLevel 100 --cellularBars 4 --wifiBars 3 2>/dev/null || echo "status bar override not available"
  xcrun simctl ui "$udid" appearance light 2>/dev/null || true
  xcrun simctl spawn "$udid" defaults write com.apple.keyboard.preferences \
    DidShowContinuousPathIntroduction -bool true 2>/dev/null || true
  xcrun simctl install "$udid" "$APP"
}

text_size() {
  local udid="$1" size="$2"
  xcrun simctl ui "$udid" content_size "$size" || echo "::warning::could not set the text size to $size"
  echo "text size is now: $(xcrun simctl ui "$udid" content_size 2>/dev/null || echo unknown)"
}

pictures() {
  local udid="$1" folder="$2"
  shift 2
  echo
  echo "==== pictures: $folder ===="
  bash Scripts/take_screens.sh "$udid" "out/$folder" "$@" || FAILED_PASSES="$FAILED_PASSES pictures:$folder"
}

# checks <label> <udid> <device word> <no-scroll 0|1> <audit 0|1> <big-text 0|1> <xcodebuild test choices...>
checks() {
  local label="$1" udid="$2" device="$3" no_scroll="$4" audit="$5" big_text="$6"
  shift 6
  echo
  echo "==== checks: $label ===="
  mkdir -p "ui-artifacts/failures/$label"
  # Variables that start with TEST_RUNNER_ reach the tests without the prefix.
  TEST_RUNNER_ZEST_SCREENS_DEVICE="$device" \
  TEST_RUNNER_ZEST_SCREENS_NO_SCROLL="$no_scroll" \
  TEST_RUNNER_ZEST_SCREENS_AUDIT="$audit" \
  TEST_RUNNER_ZEST_SCREENS_BIG_TEXT="$big_text" \
  TEST_RUNNER_ZEST_FAILURE_DIR="$PWD/ui-artifacts/failures/$label" \
  xcodebuild test-without-building \
    -project ZestVideo.xcodeproj \
    -scheme ZestVideoUITests \
    -destination "platform=iOS Simulator,id=$udid" \
    -derivedDataPath build/DerivedData \
    -resultBundlePath "${RUNNER_TEMP:-/tmp}/$label.xcresult" \
    -parallel-testing-enabled NO \
    -test-timeouts-enabled YES \
    -default-test-execution-time-allowance 300 \
    -maximum-test-execution-time-allowance 900 \
    CODE_SIGNING_ALLOWED=NO \
    "$@" 2>&1 | tee "ui-artifacts/$label.log" \
    | grep -E "^Test Case|######## (FAILED|On screen)|^NOTE |Executed [0-9]+ tests|\*\* TEST" || true
  if ! grep -q "\*\* TEST EXECUTE SUCCEEDED \*\*" "ui-artifacts/$label.log"; then
    FAILED_PASSES="$FAILED_PASSES checks:$label"
  fi
}

case "$VARIANT" in
  small)
    SMALL=$(pick small) || exit 1
    boot "$SMALL"
    pictures "$SMALL" screens-small $ALL
    pictures "$SMALL" screens-odd-names odd $ODD
    checks small "$SMALL" small 0 1 0 "-only-testing:$SUITE" "-skip-testing:$ODD_TEST"
    checks small-odd-names "$SMALL" small 0 1 0 "-only-testing:$ODD_TEST"
    ;;
  mid-ipad)
    MID=$(pick mid) || exit 1
    boot "$MID"
    pictures "$MID" screens-mid $ALL
    # On 6.1 inches and up every main action must show without scrolling.
    checks mid "$MID" mid 1 1 0 "-only-testing:$SUITE" "-skip-testing:$ODD_TEST"
    xcrun simctl shutdown "$MID" 2>/dev/null || true

    # The app is iPhone-only. On an iPad it runs in an iPhone-sized window,
    # which is how Apple's reviewer may see it.
    IPAD=$(pick ipad) || exit 1
    boot "$IPAD"
    pictures "$IPAD" screens-ipad $ALL
    checks ipad "$IPAD" ipad 0 0 0 "-only-testing:$SUITE" "-skip-testing:$ODD_TEST"
    ;;
  large-text)
    BIG=$(pick big) || exit 1
    boot "$BIG"
    # First the big phone as it comes: Apple's audit at the ordinary text size.
    checks big "$BIG" big 1 1 0 "-only-testing:$SUITE" "-skip-testing:$ODD_TEST"

    # The largest text size the app follows (it stops growing there).
    text_size "$BIG" extra-extra-extra-large
    pictures "$BIG" screens-large-text/1-largest-the-app-allows $ALL
    checks large-text "$BIG" large-text 0 1 1 "-only-testing:$SUITE" "-skip-testing:$ODD_TEST"

    # The very largest accessibility size: shows what the cap does. It should
    # look the same as the set above.
    text_size "$BIG" accessibility-extra-extra-extra-large
    pictures "$BIG" screens-large-text/2-largest-accessibility-size $ALL
    text_size "$BIG" large
    ;;
  *)
    echo "usage: screens_job.sh small | mid-ipad | large-text"
    exit 2
    ;;
esac

echo
du -sh out/* 2>/dev/null || true
if [ -n "$FAILED_PASSES" ]; then
  echo "::error::These passes failed:$FAILED_PASSES"
  exit 1
fi
echo "every pass succeeded"
