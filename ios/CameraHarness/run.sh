#!/bin/bash
# Runs the app's camera service on Linux against a capture session a script
# steers (CI job "camera"; or by hand: bash ios/CameraHarness/run.sh).
#
# The app target only compiles on a Mac, and the simulator has no camera, so
# the part of CameraService.swift a phone runs is run by nothing else. Here
# the file itself is compiled, unchanged, against small stand-ins for the
# pieces of AVFoundation and UIKit it uses, and played through what a phone
# can do to a capture session: stop it with no word, interrupt it, fail it,
# refuse to record. The file is compiled twice: as a phone compiles it
# ("Phone"), and with its simulator-only code switched on ("Simulator": the
# stand-in recorder, driven the way the UI tests drive it).
#
# What this is not: AVFoundation. It checks what the app does when the
# session behaves in a given way, never whether a real session does.
set -euo pipefail
cd "$(dirname "$0")"

if [ "$(uname)" != "Linux" ]; then
  echo "Linux only: on a Mac the real AVFoundation and UIKit are in the way. CI runs this (job \"camera\")."
  exit 0
fi

SERVICE=../ZestVideo/Services/CameraService.swift
cp "$SERVICE" Sources/Phone/CameraService.swift
sed 's/#if targetEnvironment(simulator)/#if true/' "$SERVICE" > Sources/Simulator/CameraService.swift
if ! grep -q "#if true" Sources/Simulator/CameraService.swift; then
  echo "CameraService.swift has no simulator-only code any more: the Simulator half of this harness has nothing to run."
  exit 1
fi

swift build
.build/debug/Phone
.build/debug/Simulator
