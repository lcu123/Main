#!/usr/bin/env python3
"""Picks an iOS simulator of a given kind from what this Mac has.

    python3 pick_simulator.py big      # the largest iPhone
    python3 pick_simulator.py mid      # a 6.1-inch iPhone (or the nearest to it)
    python3 pick_simulator.py small    # the smallest iPhone the newest iOS can run
    python3 pick_simulator.py ipad     # an iPad (the app runs there as an iPhone app)
    python3 pick_simulator.py --self-test

Prints the simulator's UDID on standard output and says what it chose, and
why, on standard error. Standard library only.

Sizes are read from each device type's own description on this Mac (its
screen in pixels and its scale), not guessed from names, so a new iPhone is
ranked correctly the day it appears. If the smallest iPhone the newest iOS
supports (an SE, a mini) has no simulator yet, one is created.
"""

import json
import os
import plistlib
import re
import subprocess
import sys


def simctl(*arguments):
    result = subprocess.run(["xcrun", "simctl"] + list(arguments), capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def say(text):
    sys.stderr.write(text + "\n")


def version_of(runtime_id):
    """'com.apple.CoreSimulator.SimRuntime.iOS-26-5' -> (26, 5)."""
    match = re.search(r"iOS-(\d+)(?:-(\d+))?(?:-(\d+))?", runtime_id)
    if not match:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


# Screens in points, for device types whose own description cannot be read.
# Only a fallback: the real figures come from the Mac.
KNOWN_POINTS = [
    (r"iPhone SE", (375, 667)),
    (r"iPhone \d+ mini", (375, 812)),
    (r"iPhone 1[2-4]$|iPhone 16e|iPhone 1[2-4] Pro$", (390, 844)),
    (r"iPhone 1[5-6]$|iPhone 1[5-6] Pro$", (393, 852)),
    (r"iPhone 17$|iPhone 1[6-7] Pro$", (402, 874)),
    (r"iPhone Air", (420, 912)),
    (r"Plus$", (430, 932)),
    (r"Pro Max$", (440, 956)),
    (r"iPad mini", (744, 1133)),
    (r"iPad", (820, 1180)),
]


def points_from_name(name):
    for pattern, points in KNOWN_POINTS:
        if re.search(pattern, name):
            return points
    return None


def points_from_bundle(bundle_path):
    """Reads the screen from <bundle>/Contents/Resources/profile.plist."""
    if not bundle_path:
        return None
    path = os.path.join(bundle_path, "Contents", "Resources", "profile.plist")
    try:
        with open(path, "rb") as handle:
            profile = plistlib.load(handle)
        width = float(profile["mainScreenWidth"])
        height = float(profile["mainScreenHeight"])
        scale = float(profile.get("mainScreenScale", 1)) or 1.0
    except (OSError, KeyError, ValueError, TypeError, plistlib.InvalidFileException):
        return None
    short, long = sorted((width / scale, height / scale))
    return (int(round(short)), int(round(long)))


def family_of(device_type):
    family = device_type.get("productFamily") or ""
    name = device_type.get("name") or ""
    if family == "iPad" or name.startswith("iPad"):
        return "iPad"
    if family == "iPhone" or name.startswith("iPhone"):
        return "iPhone"
    return family or "other"


def describe_types(device_types):
    """identifier -> {name, family, points} for every type whose screen is known."""
    known = {}
    for device_type in device_types:
        identifier = device_type.get("identifier")
        name = device_type.get("name") or ""
        if not identifier:
            continue
        points = points_from_bundle(device_type.get("bundlePath")) or points_from_name(name)
        if points is None:
            continue
        known[identifier] = {"identifier": identifier, "name": name, "family": family_of(device_type), "points": points}
    return known


def choose(kind, runtime_id, supported_ids, types, devices):
    """Returns (udid or None, device types to try to create (best first), why).

    - supported_ids: device type identifiers the runtime can run (may be empty
      when the Mac does not say; then only existing simulators are considered).
    - types: from describe_types.
    - devices: simulators that exist on this runtime: [{udid, name, deviceTypeIdentifier}].
    """
    family = "iPad" if kind == "ipad" else "iPhone"
    existing = []
    for device in devices:
        info = types.get(device.get("deviceTypeIdentifier"))
        if info is None:
            points = points_from_name(device.get("name") or "")
            if points is None:
                continue
            info = {"name": device.get("name"), "points": points,
                    "family": "iPad" if (device.get("name") or "").startswith("iPad") else "iPhone"}
        if info["family"] != family:
            continue
        existing.append((info["points"], device["name"], device["udid"], device.get("deviceTypeIdentifier")))
    if not existing and kind != "small" and kind != "ipad":
        return None, [], "no %s simulator exists on %s" % (family, runtime_id)

    def area(points):
        return points[0] * points[1]

    if kind == "big":
        best = max(existing, key=lambda entry: (entry[0][1], entry[0][0], entry[1]))
        return best[2], [], "the largest iPhone here"
    if kind == "mid":
        # 6.1 inches is 390 x 844 or 393 x 852 points. Nearest to that, never
        # the small 4.7-inch kind.
        candidates = [entry for entry in existing if entry[0][1] >= 800] or existing
        best = min(candidates, key=lambda entry: (abs(entry[0][1] - 848) + abs(entry[0][0] - 391), entry[1]))
        return best[2], [], "the iPhone nearest to 6.1 inches here"

    # "small" and "ipad": the smallest screen of the family that this iOS can
    # run, creating a simulator for it if there is none.
    wanted = [info for identifier, info in types.items()
              if info["family"] == family and (not supported_ids or identifier in supported_ids)]
    wanted.sort(key=lambda info: (info["points"][1], info["points"][0], info["name"]))
    if kind == "ipad":
        # Any iPad shows the same thing (the app runs in an iPhone-sized
        # window). Use one that exists; make the smallest only if none does.
        if existing:
            best = min(existing, key=lambda entry: (area(entry[0]), entry[1]))
            return best[2], [], "an iPad that is already here"
        if wanted:
            return None, [info["identifier"] for info in wanted[:4]], "no iPad simulator exists; making one"
        return None, [], "no iPad simulator exists and none can be made"

    smallest_existing = min(existing, key=lambda entry: (entry[0][1], entry[0][0], entry[1])) if existing else None
    if wanted:
        target = wanted[0]
        for entry in existing:
            if entry[3] == target["identifier"]:
                return entry[2], [], "the smallest iPhone this iOS runs (%s)" % target["name"]
        # Every type smaller than the smallest simulator that exists, smallest
        # first: the first one that can be made is used.
        smaller = [info["identifier"] for info in wanted
                   if smallest_existing is None or area(info["points"]) < area(smallest_existing[0])]
        if smaller:
            return (smallest_existing[2] if smallest_existing else None), smaller[:6], \
                "the smallest iPhone this iOS runs is %s (%d x %d points); making a simulator for it" % (
                    target["name"], target["points"][0], target["points"][1])
    if smallest_existing is not None:
        return smallest_existing[2], [], "the smallest iPhone here"
    return None, [], "no iPhone simulator exists and none can be made"


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("big", "mid", "small", "ipad", "--self-test"):
        sys.exit(__doc__)
    if sys.argv[1] == "--self-test":
        return self_test()
    kind = sys.argv[1]

    code, out, err = simctl("list", "devices", "available", "--json")
    if code != 0:
        sys.exit("simctl could not list simulators: %s" % err.strip())
    by_runtime = json.loads(out).get("devices", {})

    versions = {runtime_id: version_of(runtime_id) for runtime_id in by_runtime}
    runtimes = [runtime_id for runtime_id, version in versions.items() if version and by_runtime[runtime_id]]
    if not runtimes:
        sys.exit("No iOS simulator is installed on this Mac.")
    runtime_id = max(runtimes, key=lambda identifier: versions[identifier])
    devices = [device for device in by_runtime[runtime_id] if device.get("isAvailable", True)]

    code, out, _ = simctl("list", "devicetypes", "--json")
    device_types = json.loads(out).get("devicetypes", []) if code == 0 else []
    types = describe_types(device_types)

    supported_ids = set()
    code, out, _ = simctl("list", "runtimes", "--json")
    if code == 0:
        for runtime in json.loads(out).get("runtimes", []):
            if runtime.get("identifier") == runtime_id:
                supported_ids = {entry.get("identifier") for entry in runtime.get("supportedDeviceTypes", [])}

    if not supported_ids:
        # The Mac did not say what this iOS can run. Each device type gives the
        # range of iOS versions it supports, packed as major<<16 | minor<<8 | patch.
        version = versions[runtime_id]
        packed = (version[0] << 16) | ((version[1] if len(version) > 1 else 0) << 8)
        for device_type in device_types:
            low = device_type.get("minRuntimeVersion")
            high = device_type.get("maxRuntimeVersion")
            if isinstance(low, int) and isinstance(high, int) and low <= packed <= high:
                supported_ids.add(device_type.get("identifier"))

    udid, creates, why = choose(kind, runtime_id, supported_ids, types, devices)
    made = None
    for create in creates:
        name = "Zest %s test" % kind
        code, out, err = simctl("create", name, create, runtime_id)
        if code == 0 and out.strip():
            udid = out.strip().splitlines()[-1]
            made = create
            say("Made a new simulator: %s" % types.get(create, {}).get("name", create))
            break
        say("Could not make %s (%s)." % (types.get(create, {}).get("name", create), err.strip() or "no reason given"))
    if creates and made is None:
        say("Using a simulator that is already here instead.")
        why = "the smallest that could be had"
    if not udid:
        sys.exit("No simulator of kind '%s': %s" % (kind, why))

    chosen = next((device for device in devices if device.get("udid") == udid), None)
    label = chosen["name"] if chosen else (types.get(made, {}).get("name") or "new simulator")
    info = types.get(chosen.get("deviceTypeIdentifier")) if chosen else types.get(made)
    size = "%d x %d points" % info["points"] if info else "size unknown"
    say("Using %s, %s, %s: %s." % (label, size, runtime_id.rsplit(".", 1)[-1], why))
    print(udid)


def self_test():
    types = {
        "t.se3": {"identifier": "t.se3", "name": "iPhone SE (3rd generation)", "family": "iPhone", "points": (375, 667)},
        "t.16e": {"identifier": "t.16e", "name": "iPhone 16e", "family": "iPhone", "points": (390, 844)},
        "t.17": {"identifier": "t.17", "name": "iPhone 17", "family": "iPhone", "points": (402, 874)},
        "t.air": {"identifier": "t.air", "name": "iPhone Air", "family": "iPhone", "points": (420, 912)},
        "t.17pm": {"identifier": "t.17pm", "name": "iPhone 17 Pro Max", "family": "iPhone", "points": (440, 956)},
        "t.mini": {"identifier": "t.mini", "name": "iPad mini (A17 Pro)", "family": "iPad", "points": (744, 1133)},
        "t.ipad": {"identifier": "t.ipad", "name": "iPad (A16)", "family": "iPad", "points": (820, 1180)},
    }
    devices = [
        {"udid": "U-16E", "name": "iPhone 16e", "deviceTypeIdentifier": "t.16e"},
        {"udid": "U-17", "name": "iPhone 17", "deviceTypeIdentifier": "t.17"},
        {"udid": "U-AIR", "name": "iPhone Air", "deviceTypeIdentifier": "t.air"},
        {"udid": "U-PM", "name": "iPhone 17 Pro Max", "deviceTypeIdentifier": "t.17pm"},
        {"udid": "U-IPAD", "name": "iPad (A16)", "deviceTypeIdentifier": "t.ipad"},
    ]
    every = set(types)

    def check(condition, what):
        if not condition:
            raise AssertionError(what)

    check(choose("big", "rt", every, types, devices)[0] == "U-PM", "big is the Pro Max")
    check(choose("mid", "rt", every, types, devices)[0] == "U-16E", "mid is the 6.1-inch one")
    # The SE can run but has no simulator: make it, with the smallest existing as the fallback.
    udid, create, _ = choose("small", "rt", every, types, devices)
    check(create == ["t.se3"] and udid == "U-16E", "small asks for an SE and falls back to the 16e")
    # This iOS cannot run the SE: the smallest existing iPhone it is.
    udid, create, _ = choose("small", "rt", every - {"t.se3"}, types, devices)
    check(udid == "U-16E" and create == [], "small without an SE is the 16e")
    # The Mac does not say what the runtime supports: the smallest of all known types is tried.
    check(choose("small", "rt", set(), types, devices)[1] == ["t.se3"], "small with no list still asks for the SE")
    # An SE simulator already exists: use it.
    with_se = devices + [{"udid": "U-SE", "name": "iPhone SE (3rd generation)", "deviceTypeIdentifier": "t.se3"}]
    check(choose("small", "rt", every, types, with_se)[:2] == ("U-SE", []), "an existing SE is used")
    check(choose("ipad", "rt", every, types, devices)[0] == "U-IPAD", "an existing iPad is used")
    no_ipad = [device for device in devices if not device["name"].startswith("iPad")]
    check(choose("ipad", "rt", every, types, no_ipad)[1] == ["t.mini", "t.ipad"], "with no iPad, the smallest is tried first")
    # A device whose type is unknown is ranked by its name.
    unknown = [{"udid": "U-X", "name": "iPhone 17 Pro Max", "deviceTypeIdentifier": "t.new"},
               {"udid": "U-Y", "name": "iPhone 16e", "deviceTypeIdentifier": "t.other"}]
    check(choose("big", "rt", set(), {}, unknown)[0] == "U-X", "names rank devices when types are unknown")
    check(choose("mid", "rt", set(), {}, unknown)[0] == "U-Y", "and pick the mid one")
    check(version_of("com.apple.CoreSimulator.SimRuntime.iOS-26-5") == (26, 5), "runtime version")
    check(version_of("com.apple.CoreSimulator.SimRuntime.iOS-18-3-1") == (18, 3, 1), "three-part version")
    check(version_of("com.apple.CoreSimulator.SimRuntime.watchOS-11-0") is None, "not iOS")
    check(points_from_name("iPhone SE (3rd generation)") == (375, 667), "name table")
    print("self-test passed")


if __name__ == "__main__":
    main()
