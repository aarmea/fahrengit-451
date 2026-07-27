#!/usr/bin/env python3
"""
test_sync.py
────────────────────────────────────────────────────────────────────────────
Manual integration test for sync.py. Not run by anything automatically: it
talks to a real Forgejo instance, downloads real (large) release assets, and
needs fdroidserver on the machine running it.

    sudo apt install --no-install-recommends fdroidserver default-jdk-headless
    FORGEJO_URL=https://git.example.com TEST_REPO=alice/my-app \\
        python3 fdroid/test_sync.py

It redirects sync.py's container paths into a temp tree, so it touches neither
the volume nor the live repository. Everything it does over the network is a
GET against public endpoints.

The source repo named by TEST_REPO must already carry its F-Droid metadata at
its latest release tag (TEST_METADATA_PATH, default dist/fdroid/metadata) —
that is what the service reads in production.

What it covers, and why each case exists:

    1. latest_release()   — prereleases must never reach devices
    2. fetch_metadata()   — listings come from the release tag, not from HEAD
    3. missing metadata   — must be a hard error, not a silent empty publish
    4. the happy path     — download → fdroid update → verify → atomic publish
    5. a wrong signing key — must block publication AND leave the live repo
                             untouched (this one caught two real bugs)
    6. recovery + pruning — the next poll re-downloads what was rejected
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import sync  # noqa: E402

REPO_UNDER_TEST = os.environ.get("TEST_REPO", "")
METADATA_PATH   = os.environ.get("TEST_METADATA_PATH", "dist/fdroid/metadata")

if not REPO_UNDER_TEST:
    sys.exit("TEST_REPO is unset — e.g. TEST_REPO=alice/my-app python3 test_sync.py")

TMP = Path(tempfile.mkdtemp(prefix="fdroid-test-"))

sync.SRV        = TMP
sync.WORK       = TMP / "work"
sync.REPO       = sync.WORK / "repo"
sync.METADATA   = sync.WORK / "metadata"
sync.STATE_FILE = sync.WORK / "state.json"
sync.KEYSTORE   = TMP / "keystore.jks"
sync.KEYSTOREPASS = sync.KEYPASS = "testtest"

for path in (sync.WORK, sync.REPO, sync.METADATA):
    path.mkdir(parents=True, exist_ok=True)

subprocess.run(
    ["keytool", "-genkeypair", "-noprompt", "-keystore", str(sync.KEYSTORE),
     "-alias", sync.KEYALIAS, "-keyalg", "RSA", "-keysize", "2048",
     "-validity", "30", "-storepass", "testtest", "-keypass", "testtest",
     "-dname", "CN=test"], check=True, capture_output=True)

CFG = {
    "repo_url": "https://localhost/fdroid/repo",
    "repo_name": "Test",
    "repo_description": "integration test",
    "poll_interval_minutes": 15,
    "sources": [{"repo": REPO_UNDER_TEST, "metadata_path": METADATA_PATH,
                 "asset_glob": "*.apk"}],
}


def banner(text: str) -> None:
    print("=" * 70)
    print(text)


banner("TEST 1: latest_release() skips prereleases")
release = sync.latest_release(REPO_UNDER_TEST)
assert release and not release["prerelease"], release
tag = release["tag_name"]
print(f"  OK: {tag}, {len(release['assets'])} assets")

banner("TEST 2: fetch_metadata() reads the source repo at the release tag")
count = sync.fetch_metadata(REPO_UNDER_TEST, tag, METADATA_PATH)
print(f"  OK: {count} metadata file(s) from {tag}")

banner("TEST 3: missing metadata is a hard error")
try:
    sync.fetch_metadata(REPO_UNDER_TEST, tag, "no/such/dir")
    raise AssertionError("should have raised")
except RuntimeError as exc:
    print(f"  OK: {exc}")
sync.fetch_metadata(REPO_UNDER_TEST, tag, METADATA_PATH)

banner("TEST 4: download → fdroid update → verify → publish")
apks = sync.download_assets(release, "*.apk")
sync.write_fdroid_config(CFG)
sync.run_fdroid_update()
sync.verify_published(apks)
sync.publish(tag)

live = TMP / "repo"
assert live.is_symlink(), "repo is not a symlink"
assert (live / "index-v2.json").exists()
assert (live / "index.png").exists(), "no QR code was generated"
print(f"  OK: {live.name} -> {live.readlink()}, {len(apks)} APK(s) indexed")

banner("TEST 5: a wrongly-signed APK blocks publication")
pinned = [p for p in sync.METADATA.glob("*.yml")
          if "AllowedAPKSigningKeys" in p.read_text()]
if not pinned:
    print("  SKIPPED: no metadata file pins AllowedAPKSigningKeys.")
    print("           Add it — without a pin, any key can be republished.")
else:
    target = pinned[0]
    original = target.read_text()
    # Flip one hex digit of the pin: still well-formed, no longer the real key.
    target.write_text(re.sub(r"\b([a-f0-9]{64})\b",
                             lambda m: ("a" if m.group(1)[0] != "a" else "b") + m.group(1)[1:],
                             original, count=1))
    sync.run_fdroid_update()
    try:
        sync.verify_published(apks)
        raise AssertionError("verify_published should have refused")
    except RuntimeError as exc:
        print(f"  OK: refused to publish: {str(exc)[:100]}...")

    # The point of the exercise: the previously published repo is untouched.
    index = (live / "index-v2.json").read_text()
    for name in apks:
        assert name in index, f"{name} vanished from the LIVE index"
    print("  OK: the live snapshot is still complete")
    target.write_text(original)

banner("TEST 6: the next poll recovers; snapshots prune")
again = sync.download_assets(release, "*.apk")
assert set(again) == set(apks)
sync.run_fdroid_update()
sync.verify_published(again)
for fake in ("v0.0.1", "v0.0.2", "v0.0.3", "v0.0.4"):
    sync.publish(fake)
kept = sorted(p.name for p in TMP.glob("repo-*"))
assert len(kept) == sync.KEEP_SNAPSHOTS, kept
print(f"  OK: kept {kept}, live -> {(TMP / 'repo').readlink()}")

banner("ALL TESTS PASSED")
shutil.rmtree(TMP, ignore_errors=True)
