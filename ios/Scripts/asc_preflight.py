#!/usr/bin/env python3
"""Ask App Store Connect, before the ten-minute build, whether the upload can work.

Run by the testflight job in .github/workflows/ios.yml. Reads the key from
ASC_KEY_PATH, ASC_KEY_ID, ASC_ISSUER_ID, and checks, in a few seconds:

  1. the key is accepted (a wrong key ID, issuer or .p8 is a 401 here, not at minute 12);
  2. no agreement is waiting for the Account Holder (a 403 that says so);
  3. the app record for BUNDLE_ID exists in App Store Connect;
  4. the App ID exists and belongs to APPLE_TEAM_ID;
  5. Push Notifications is switched on for the App ID, and switches it on if
     it is not (the app is entitled to aps-environment, and the App Store
     profile Xcode makes at export must carry it).

Writes PUSH_READY=yes or PUSH_READY=no to GITHUB_ENV; the Archive step keeps the
app's entitlements only on yes. Exit 1 means the upload cannot work and says
why. An answer that is not about this app (Apple's API down, a 400) does not
stop the run: it goes on with PUSH_READY=no, which always uploads.
Never prints the key. Needs only python3, openssl and curl.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse

API = os.environ.get("ASC_API", "https://api.appstoreconnect.apple.com")


def b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def der_to_raw(der):
    """An ECDSA signature as openssl writes it (DER) to the 64 bytes a JWT wants."""
    if len(der) < 8 or der[0] != 0x30:
        raise ValueError("not a DER signature")
    at = 2 if der[1] < 0x80 else 2 + (der[1] & 0x7F)
    parts = []
    for _ in range(2):
        if der[at] != 0x02:
            raise ValueError("not a DER signature")
        size = der[at + 1]
        number = der[at + 2:at + 2 + size].lstrip(b"\x00")
        if len(number) > 32:
            raise ValueError("not a P-256 signature")
        parts.append(number.rjust(32, b"\x00"))
        at += 2 + size
    return parts[0] + parts[1]


def token():
    now = int(time.time())
    head = b64url(json.dumps({"alg": "ES256", "kid": os.environ["ASC_KEY_ID"], "typ": "JWT"}).encode())
    body = b64url(json.dumps({"iss": os.environ["ASC_ISSUER_ID"], "iat": now - 30, "exp": now + 600,
                              "aud": "appstoreconnect-v1"}).encode())
    signed = subprocess.run(["openssl", "dgst", "-sha256", "-sign", os.environ["ASC_KEY_PATH"]],
                            input=(head + "." + body).encode(), capture_output=True)
    if signed.returncode != 0:
        fail("openssl could not sign with the .p8 file. The secret ASC_KEY_P8 must be the whole "
             "file, from -----BEGIN PRIVATE KEY----- to -----END PRIVATE KEY-----.")
    try:
        return head + "." + body + "." + b64url(der_to_raw(signed.stdout))
    except (ValueError, IndexError):
        fail("The .p8 file is a key, but not an App Store Connect API key (those are P-256 keys). "
             "Save the downloaded AuthKey_XXXX.p8 as the secret ASC_KEY_P8.")


def say(kind, text):
    print("::%s::%s" % (kind, text.replace("\n", " ")), flush=True)


def fail(text):
    say("error", text)
    sys.exit(1)


def call(method, path, body=None):
    """Returns (status, parsed JSON or {}). Never raises for an HTTP status.

    Goes through curl, which uses the Mac's own certificate store; the token
    and the body reach it on standard input, never on the command line."""
    last = "no answer"
    for attempt in range(3):
        with tempfile.TemporaryDirectory() as box:
            out = os.path.join(box, "answer.json")
            config = ['url = "%s"' % (API + path), 'request = "%s"' % method, "globoff", "silent", "show-error",
                      "max-time = 30", 'output = "%s"' % out, 'write-out = "%{http_code}"',
                      'header = "Authorization: Bearer %s"' % token(), 'header = "Content-Type: application/json"']
            if body is not None:
                sent = os.path.join(box, "body.json")
                with open(sent, "w") as handle:
                    json.dump(body, handle)
                config.append('data-binary = "@%s"' % sent)
            done = subprocess.run(["curl", "--config", "-"], input="\n".join(config) + "\n",
                                  capture_output=True, text=True)
            code = done.stdout.strip()[-3:]
            status = int(code) if code.isdigit() else 0
            raw = b""
            if os.path.exists(out):
                with open(out, "rb") as handle:
                    raw = handle.read()
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            parsed = {}
        if status and status not in (429, 500, 502, 503, 504):
            return status, parsed
        last = "HTTP %s" % status if status else (done.stderr.strip() or "no answer")
        if attempt < 2:
            time.sleep(3 * (attempt + 1))
    if status:
        return status, parsed
    return 0, {"errors": [{"code": "NO_ANSWER", "detail": last}]}


def why(parsed):
    errors = parsed.get("errors") or []
    return "; ".join("%s: %s" % (e.get("code"), e.get("detail") or e.get("title")) for e in errors) or "no detail"


def answered(status, parsed, what):
    """True on a 200. A refusal of the key or a waiting agreement ends the run: the
    upload would be refused the same way. Anything else is not an answer about this
    app, so the run goes on, without the entitlements to be safe (see main)."""
    if status == 200:
        return True
    if status == 401:
        fail("App Store Connect refused the key (401) while %s. Check that the secrets ASC_KEY_ID, "
             "ASC_ISSUER_ID and ASC_KEY_P8 belong to the same key, that it is a Team key (Users and Access, "
             "Integrations, Team Keys; an Individual key does not work here) and that it is not revoked. %s"
             % (what, why(parsed)))
    if status == 403:
        fail("App Store Connect said no (403) while %s. Its words: %s. If they name an agreement, the Account "
             "Holder must accept it at developer.apple.com/account and appstoreconnect.apple.com. If not, the "
             "key may not do this: it must be a Team key with the Admin role." % (what, why(parsed)))
    say("warning", "App Store Connect answered %s while %s (%s). Going on without knowing; this build is "
        "archived without its entitlements, so it uploads but gets no pushes." % (status or "nothing", what, why(parsed)))
    return False


def finish(push_ready):
    with open(os.environ.get("GITHUB_ENV") or os.devnull, "a") as env:
        env.write("PUSH_READY=%s\n" % push_ready)
    print("PUSH_READY=%s" % push_ready)


def main():
    bundle = os.environ["BUNDLE_ID"]
    team = os.environ.get("APPLE_TEAM_ID", "")
    push_ready = "no"

    # 1-3. The key works, no agreement is waiting, the app record is there.
    query = urllib.parse.urlencode({"filter[bundleId]": bundle, "fields[apps]": "bundleId,name", "limit": "50"})
    status, parsed = call("GET", "/v1/apps?" + query)
    if not answered(status, parsed, "looking for the app record"):
        return finish("no")
    apps = [a for a in parsed.get("data", []) if a.get("attributes", {}).get("bundleId") == bundle]
    if not apps:
        near = sorted({a.get("attributes", {}).get("bundleId") or "?" for a in parsed.get("data", [])})
        fail("There is no app in App Store Connect with bundle ID %s. Create it (Apps, +, New App) and run again.%s"
             % (bundle, " The nearest it has: %s." % ", ".join(near) if near else ""))
    print("App record: %s (%s)" % (apps[0]["attributes"].get("name"), bundle))

    # 4. The App ID, and whose it is.
    query = urllib.parse.urlencode({"filter[identifier]": bundle, "include": "bundleIdCapabilities",
                                    "fields[bundleIds]": "identifier,platform,seedId,bundleIdCapabilities",
                                    "limit[bundleIdCapabilities]": "50", "limit": "200"})
    status, parsed = call("GET", "/v1/bundleIds?" + query)
    if not answered(status, parsed, "looking for the App ID"):
        return finish("no")
    ids = [b for b in parsed.get("data", []) if b.get("attributes", {}).get("identifier") == bundle]
    if not ids:
        say("warning", "App Store Connect did not list the App ID %s although the app record exists. Going on "
            "without knowing; this build is archived without its entitlements, so it uploads but gets no pushes."
            % bundle)
        return finish("no")
    app_id = ids[0]
    seed = app_id["attributes"].get("seedId") or ""
    if team and seed and seed != team:
        fail("The secret APPLE_TEAM_ID is not the team that owns %s: the App ID's prefix is another one. "
             "Copy the Team ID from developer.apple.com, Membership details." % bundle)
    mine = {r["id"] for r in app_id.get("relationships", {}).get("bundleIdCapabilities", {}).get("data", []) or []}
    switched_on = sorted(
        c.get("attributes", {}).get("capabilityType") or "?"
        for c in parsed.get("included", [])
        if c.get("type") == "bundleIdCapabilities" and (not mine or c.get("id") in mine))
    print("App ID capabilities: %s" % (", ".join(switched_on) or "none"))

    # 5. Push Notifications.
    if "PUSH_NOTIFICATIONS" in switched_on:
        push_ready = "yes"
    else:
        status, answer = call("POST", "/v1/bundleIdCapabilities", {"data": {
            "type": "bundleIdCapabilities",
            "attributes": {"capabilityType": "PUSH_NOTIFICATIONS"},
            "relationships": {"bundleId": {"data": {"type": "bundleIds", "id": app_id["id"]}}}}})
        if status in (200, 201):
            push_ready = "yes"
            say("notice", "Push Notifications was off for the App ID %s; it is switched on now." % bundle)
        else:
            say("warning", "Push Notifications is off for the App ID %s and could not be switched on (%s %s). "
                "This build is archived without its entitlements, so it uploads but gets no pushes. Tick Push "
                "Notifications for the App ID at developer.apple.com, Identifiers, and run again."
                % (bundle, status, why(answer)))

    # For the record only: a team with no registered device cannot be signed for development.
    status, parsed = call("GET", "/v1/devices?limit=1")
    if status == 200:
        print("Registered devices: %s" % ("at least one" if parsed.get("data") else "none (expected; none is needed)"))

    finish(push_ready)


if __name__ == "__main__":
    main()
