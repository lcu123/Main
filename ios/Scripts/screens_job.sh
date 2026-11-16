#!/bin/bash
# What one "screen sizes" CI job does: boot the right simulators, set the text
# size, take the pictures, and run the layout and accessibility checks
# (ScreensUITests) on each. Run from the ios/ folder, after the app and the UI
# tests have been built into build/DerivedData.
#
#   screens_job.sh small        the smallest iPhone: usual route, then the awkward names,
#                               then the two sign-in steps with the keyboard up at
#                               the largest text
#   screens_job.sh mid-ipad     a 6.1-inch iPhone (and its sign-in steps with the
#                               keyboard up at the largest text), then an iPad
#   screens_job.sh large-text   the big iPhone with the text at the largest size the
#                               app follows: pictures and every check
#   screens_job.sh big          the big iPhone: every check at the ordinary text
#                               size, then pictures and the layout checks at the
#                               very largest accessibility size
#
# Pictures go to out/<artifact name>/, logs and reports to ui-artifacts/.
# Every pass is run even if an earlier one failed; the exit code says whether
# any did.
#
# A pass can end badly with no test failing: a test that sat out its time, a
# test runner that never started. Three things deal with that:
#   - a watcher looks at the pass's log while it runs. If a test has written
#     nothing for STALL_SECONDS it is stuck: the watcher saves what the app
#     and the test runner were doing (a stack sample) and closes the app, so
#     the test ends with a reason instead of sitting out its five minutes;
#   - after the pass, the reason is printed ("######## TIMED OUT ...");
#   - a pass whose test runner never started is run once more on a freshly
#     started simulator, and a simulator that had a stuck test is restarted
#     before the next pass.
set -u

VARIANT="${1:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/.."

APP="build/DerivedData/Build/Products/Debug-iphonesimulator/ZestVideo.app"
# One test per screen with the usual customers; and the awkward names, in
# seven tests of a few minutes each.
SUITE="ZestVideoUITests/ScreensUITests"
ODD_SUITE="ZestVideoUITests/OddNamesUITests"
# The last two are the sign-in steps again, with the cursor in the field and
# so the keyboard up: the way a tech has them.
ALL="login code today today2 confirm far record recording review warning saved stopvideo locked queue update team lowspace full olddraft loginkeys codekeys"
# The awkward names: the lists, then the screen with the largest type for
# every customer, then the other screens for the four longest names.
ODD="today today2 queue queue@4 queue@8"
ODD="$ODD confirm confirm@1 confirm@2 confirm@3 confirm@4 confirm@5 confirm@6 confirm@7 confirm@8 confirm@9"
for index in 2 4 5 9; do
  ODD="$ODD record@$index review@$index warning@$index stopvideo@$index locked@$index team@$index"
done

mkdir -p ui-artifacts/failures out
FAILED_PASSES=""
BUNDLE=com.zestlawnpest.video
# A test that writes nothing for this long is stuck. (The longest quiet spell
# in a healthy run is under 20 seconds.)
STALL_SECONDS=75
# The text size the simulator was last set to ("" = as it comes).
TEXT_SIZE=""

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

# The on-screen keyboard has to show: the sign-in steps are pictured and
# checked with it up (loginkeys, codekeys, the two "keyboard up" tests). A
# simulator that thinks a hardware keyboard is connected shows none. The
# setting is the Simulator app's own; it is read when a simulator boots, so
# it is written before every boot: for all simulators, and for this one.
soft_keyboard() {
  local udid="$1"
  defaults write com.apple.iphonesimulator ConnectHardwareKeyboard -bool false 2>/dev/null \
    || echo "::warning::could not switch the simulator's hardware keyboard off: the keyboard-up checks may be skipped"
  defaults write com.apple.iphonesimulator DevicePreferences -dict-add "$udid" \
    '<dict><key>ConnectHardwareKeyboard</key><false/></dict>' 2>/dev/null || true
}

boot() {
  local udid="$1"
  soft_keyboard "$udid"
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

# Shut the simulator down and start it again, as it was.
restart() {
  local udid="$1"
  xcrun simctl shutdown "$udid" 2>/dev/null || true
  boot "$udid"
  if [ -n "$TEXT_SIZE" ]; then
    xcrun simctl ui "$udid" content_size "$TEXT_SIZE" 2>/dev/null || true
  fi
}

text_size() {
  local udid="$1" size="$2"
  TEXT_SIZE="$size"
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

# The main thread of a sampled process: the part of the sample worth reading.
main_thread() {
  awk '/^Call graph:/{on=1; next} on && /^    [0-9]+ Thread_/{n++; if (n>1) exit} on' "$1" | cut -c1-200 | tail -n 45
}

# watch_for_stalls <label> <udid> <log>   (runs beside xcodebuild)
watch_for_stalls() {
  local label="$1" udid="$2" log="$3" last="" quiet=0 size started ended test count=0 pid before=""
  while :; do
    sleep 5
    size=$(wc -c < "$log" 2>/dev/null | tr -d ' ')
    if [ "$size" != "$last" ]; then last="$size"; quiet=0; continue; fi
    quiet=$((quiet + 5))
    [ "$quiet" -lt "$STALL_SECONDS" ] && continue
    quiet=0
    # Only a test that is running can be stuck; between tests, and while
    # the runner starts, there is nothing of ours to close.
    started=$(grep -n "^Test Case .* started\\." "$log" | tail -1)
    ended=$(grep -nE "^Test Case .* (passed|failed|exceeded)" "$log" | tail -1)
    [ -z "$started" ] && continue
    [ "${ended%%:*}" -gt "${started%%:*}" ] 2>/dev/null && continue
    test=$(echo "$started" | sed -E "s/.*-\\[[A-Za-z]+\\.([A-Za-z]+) ([A-Za-z0-9_]+)\\].*/\\1.\\2/")
    count=$((count + 1))
    if [ "$test" = "$before" ] && [ -z "$(pgrep -f "ZestVideo.app/ZestVideo" | head -1)" ]; then
      # The app was closed last time and the test has still not moved: it
      # is the test runner that is stuck. Stop it; xcodebuild reports the
      # test as crashed and goes on with the next one.
      pid=$(pgrep -f "ZestVideoUITests-Runner" | head -1)
      echo "######## STUCK $test: still nothing, $STALL_SECONDS seconds after the app was closed. Stopping the test runner."
      echo "STUCK $test: still stuck after the app was closed; the test runner was stopped" >> "ui-artifacts/$label.stuck.txt"
      if [ -n "$pid" ]; then
        local runner="ui-artifacts/failures/$label/stuck-$count-runner.sample.info.txt"
        sample "$pid" 2 -mayDie -file "$runner" >/dev/null 2>&1 && [ -s "$runner" ] && main_thread "$runner"
        kill -9 "$pid" 2>/dev/null
      fi
      before=""
      continue
    fi
    before="$test"
    echo "######## STUCK $test: nothing has happened for $STALL_SECONDS seconds."
    echo "######## Last thing it did: $(grep -E '^ +t = ' "$log" | tail -1 | sed -E 's/^ +//' | cut -c1-160)"
    echo "STUCK $test after: $(grep -E '^ +t = ' "$log" | tail -1 | sed -E 's/^ +//' | cut -c1-200)" >> "ui-artifacts/$label.stuck.txt"
    for name in "ZestVideo.app/ZestVideo" "ZestVideoUITests-Runner"; do
      pid=$(pgrep -f "$name" | head -1)
      [ -z "$pid" ] && { echo "######## (no $name process to look at)"; continue; }
      local file="ui-artifacts/failures/$label/stuck-$count-$(basename "$name").sample.info.txt"
      if sample "$pid" 2 -mayDie -file "$file" >/dev/null 2>&1 && [ -s "$file" ]; then
        echo "######## What $(basename "$name") was doing (its main thread; the whole sample is in $file):"
        main_thread "$file"
      else
        echo "######## (could not sample $name, pid $pid)"
      fi
    done
    echo "######## Closing the app so the test can end and say why."
    xcrun simctl terminate "$udid" "$BUNDLE" 2>/dev/null || echo "######## (the app was not running)"
  done
}

# Why a pass failed when no test in it did.
why() {
  local log="$1"
  grep -E "exceeded execution time allowance" "$log" \
    | sed -E "s/^Test Case '-\\[[A-Za-z]+\\.([A-Za-z]+) ([A-Za-z0-9_]+)\\]' exceeded execution time allowance of ([0-9a-z ]+)\\..*/######## TIMED OUT \\1.\\2: it ran for \\3 and was stopped. (If it sat still, a STUCK line above says where. If there is none, it was working and is simply too long for its allowance.)/" | sort -u
  grep -E "Failed to initialize for UI testing|encountered an error \\(|Restarting after unexpected exit|Timed out waiting for AX|Test runner never began|The test runner (exited|crashed|hung)|Unable to (boot|launch|install)|Application failed preflight|Crash: " "$log" \
    | sed -E 's/^[0-9: .+-]+ZestVideoUITests-Runner\[[0-9:]+\] //; s/^[[:space:]]+//' | sort -u | cut -c1-260 | sed 's/^/######## /'
}

# run_checks: one xcodebuild pass, with the watcher beside it.
run_checks() {
  local label="$1" udid="$2" device="$3" no_scroll="$4" audit="$5" big_text="$6"
  shift 6
  rm -rf "${RUNNER_TEMP:-/tmp}/$label.xcresult"
  : > "ui-artifacts/$label.log"
  watch_for_stalls "$label" "$udid" "ui-artifacts/$label.log" &
  local watcher=$!
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
    "$@" 2>&1 | tee -a "ui-artifacts/$label.log" \
    | grep --line-buffered -E "^Test Case|######## (FAILED|On screen)|^NOTE |Executed [0-9]+ tests|\*\* TEST" || true
  kill "$watcher" 2>/dev/null
  wait "$watcher" 2>/dev/null
  grep -q "\*\* TEST EXECUTE SUCCEEDED \*\*" "ui-artifacts/$label.log"
}

# checks <label> <udid> <device word> <no-scroll 0|1> <audit 0|1> <big-text 0|1> <xcodebuild test choices...>
checks() {
  local label="$1" udid="$2"
  local log="ui-artifacts/$label.log"
  echo
  echo "==== checks: $label ===="
  mkdir -p "ui-artifacts/failures/$label"
  rm -f "ui-artifacts/$label.stuck.txt"
  local ok=1
  run_checks "$@" || ok=0
  if [ "$ok" = 0 ] && ! grep -q "^Test Case " "$log"; then
    # Not one test started: the simulator, not the app. Once more, on a
    # simulator started afresh.
    echo "::warning::$label: the test runner never started a test. Why:"
    why "$log"
    echo "Starting the simulator again and running this pass once more."
    cp "$log" "ui-artifacts/$label.first-try.log"
    restart "$udid"
    ok=1
    run_checks "$@" || ok=0
  fi
  if [ "$ok" = 0 ]; then
    FAILED_PASSES="$FAILED_PASSES checks:$label"
    why "$log"
    if ! grep -qE "^Test Case .* failed|exceeded execution time allowance" "$log"; then
      echo "######## $label failed, and no test in it failed or timed out. The end of its log:"
      grep -vE "^ +t = |^$" "$log" | tail -25 | cut -c1-260
    fi
  fi
  # Every line of text Apple's audit flagged, measured again from the pixels
  # the tests kept. (Wrong = let through by the tests, but faint here.)
  python3 Scripts/measure_contrast.py "ui-artifacts/failures/$label" || FAILED_PASSES="$FAILED_PASSES contrast:$label"
  # A simulator that has had a stuck test is not trusted for the next pass.
  if grep -qE "exceeded execution time allowance|Restarting after unexpected exit" "$log" || [ -s "ui-artifacts/$label.stuck.txt" ]; then
    echo "A test was stopped in this pass: starting the simulator again before the next one."
    restart "$udid"
  fi
}

# keys_at_largest_text <label> <udid> <device word>
# The two sign-in steps with the keyboard up, once more with the text at the
# largest size the app follows. That is where the room over the keyboard is
# shortest (the question and the help line take two lines each), and the
# large-text job only has the biggest iPhone, where everything fits easily.
# Two tests, no pictures and no audit: a minute or two. Each test keeps a
# picture of the step with its reports (keyboard-up.<screen>.<device>.info).
keys_at_largest_text() {
  local label="$1" udid="$2" device="$3"
  text_size "$udid" extra-extra-extra-large
  checks "$label" "$udid" "$device" 0 0 1 \
    "-only-testing:$SUITE/testLoginWithTheKeyboardUp" "-only-testing:$SUITE/testCodeWithTheKeyboardUp"
  text_size "$udid" large
}

case "$VARIANT" in
  small)
    SMALL=$(pick small) || exit 1
    boot "$SMALL"
    pictures "$SMALL" screens-small $ALL
    pictures "$SMALL" screens-odd-names odd $ODD
    checks small "$SMALL" small 0 1 0 "-only-testing:$SUITE"
    checks small-odd-names "$SMALL" small 0 1 0 "-only-testing:$ODD_SUITE"
    # Last, so that the text size is changed after everything else is done.
    keys_at_largest_text small-large-text-keys "$SMALL" small-large-text
    ;;
  mid-ipad)
    MID=$(pick mid) || exit 1
    boot "$MID"
    pictures "$MID" screens-mid $ALL
    # On 6.1 inches and up every main action must show without scrolling.
    checks mid "$MID" mid 1 1 0 "-only-testing:$SUITE"
    keys_at_largest_text mid-large-text-keys "$MID" mid-large-text
    xcrun simctl shutdown "$MID" 2>/dev/null || true

    # The app is iPhone-only. On an iPad it runs in an iPhone-sized window,
    # which is how Apple's reviewer may see it.
    IPAD=$(pick ipad) || exit 1
    boot "$IPAD"
    pictures "$IPAD" screens-ipad $ALL
    checks ipad "$IPAD" ipad 0 0 0 "-only-testing:$SUITE"
    ;;
  large-text)
    BIG=$(pick big) || exit 1
    boot "$BIG"
    # The largest text size the app follows (it stops growing there):
    # pictures, then every check, Apple's audit included.
    text_size "$BIG" extra-extra-extra-large
    pictures "$BIG" screens-large-text $ALL
    checks large-text "$BIG" large-text 0 1 1 "-only-testing:$SUITE"
    text_size "$BIG" large
    ;;
  big)
    BIG=$(pick big) || exit 1
    boot "$BIG"
    # The big phone as it comes: Apple's audit at the ordinary text size.
    checks big "$BIG" big 1 1 0 "-only-testing:$SUITE"

    # The very largest accessibility size. The app stops growing one step
    # sooner, so these pictures should look like the large-text job's; the
    # layout checks run again here to prove it (every page scrolls clear,
    # what has to be read is in view). Apple's audit is not run a third
    # time on the same layout.
    text_size "$BIG" accessibility-extra-extra-extra-large
    pictures "$BIG" screens-largest-accessibility $ALL
    checks largest-accessibility "$BIG" largest-accessibility 0 0 1 "-only-testing:$SUITE"
    text_size "$BIG" large
    ;;
  *)
    echo "usage: screens_job.sh small | mid-ipad | large-text | big"
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
