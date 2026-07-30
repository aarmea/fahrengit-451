#!/usr/bin/env python3
"""
sync.py
────────────────────────────────────────────────────────────────────────────
Publishes an F-Droid repository from the APK assets attached to Forgejo
releases, so Android apps hosted here can be installed and updated from a
phone instead of being sideloaded by hand.

Every poll, for each configured source repository:

    1. ask Forgejo for the latest release
    2. skip prereleases, and skip tags already published
    3. fetch that repo's F-Droid metadata *at the release tag*
    4. download the release's .apk assets
    5. regenerate the index with `fdroid update`
    6. verify every APK actually reached the index  ← see _verify_published
    7. swap it into place atomically

Forgejo is reached over the compose network at http://forgejo:3000 — no TLS,
no geo-block, no credentials, and no public egress. Nothing here authenticates
to anything; it only reads public releases.

Layout inside the fdroid_repo volume
────────────────────────────────────
/srv/fdroid/work/config.yml       generated per run from /app/config/fdroid.yml
/srv/fdroid/work/metadata/*.yml   app listings, from the source repo's tag
/srv/fdroid/work/repo/            APKs accumulate here across releases
/srv/fdroid/work/state.json       last published tag + asset fingerprint per source
/srv/fdroid/repo-<tag>/           published snapshots (hardlinks; last 3 kept)
/srv/fdroid/repo                  symlink → the live snapshot; nginx serves this

Keeping work/repo/ across runs is what lets old versions stay installable:
`fdroid update` rebuilds the index from whatever APKs are present.
"""

import fnmatch
import io
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [fdroid] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

CONFIG_FILE   = Path("/app/config/fdroid.yml")
SRV           = Path("/srv/fdroid")
WORK          = SRV / "work"
REPO          = WORK / "repo"
METADATA      = WORK / "metadata"
STATE_FILE    = WORK / "state.json"
KEYSTORE      = Path("/keystore.jks")

FORGEJO       = os.environ.get("FORGEJO_URL", "http://forgejo:3000").rstrip("/")
KEYSTOREPASS  = os.environ.get("FDROID_KEYSTOREPASS", "")
KEYPASS       = os.environ.get("FDROID_KEYPASS", "")
KEYALIAS      = os.environ.get("FDROID_KEYALIAS", "fdroid-index")
DOMAIN        = os.environ.get("DOMAIN", "")

KEEP_SNAPSHOTS = 3
HTTP_TIMEOUT   = 120


# ── Config & state ────────────────────────────────────────────────────────────

def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"{CONFIG_FILE} not found — copy config/fdroid.yml.example to "
            "config/fdroid.yml and edit it"
        )
    cfg = yaml.safe_load(CONFIG_FILE.read_text()) or {}
    cfg.setdefault("repo_url", f"https://{DOMAIN}/fdroid/repo" if DOMAIN else "")
    cfg.setdefault("repo_name", "Repository")
    cfg.setdefault("repo_description", "")
    cfg.setdefault("poll_interval_minutes", 15)
    cfg.setdefault("sources", [])
    if not cfg["repo_url"]:
        raise ValueError("repo_url is unset and DOMAIN is empty — cannot build an index")
    return cfg


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("%s is corrupt — treating every source as unpublished.", STATE_FILE)
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


# ── Forgejo ───────────────────────────────────────────────────────────────────

def internal_url(url: str) -> str:
    """
    Rewrite a Forgejo-issued absolute URL onto the internal service address.

    Release assets come back from the API as `browser_download_url`, which is
    built from Forgejo's ROOT_URL — i.e. the *public* https://<domain>/… address.
    Following that from inside the compose network means trying to reach the
    host's own public IP from a container on the bridge, which is refused
    (there is no hairpin route back in). It would also put the download through
    nginx and its geo-blocking, for no reason: the file is on this machine's
    disk, one hop away at http://forgejo:3000.

    Only the scheme and host change; the path is Forgejo's own route and is
    served identically on the internal port.
    """
    parts = urlsplit(url)
    base = urlsplit(FORGEJO)
    return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, ""))


def latest_release(repo: str) -> dict[str, Any] | None:
    """The newest non-draft release, or None. Prereleases are skipped."""
    url = f"{FORGEJO}/api/v1/repos/{repo}/releases/latest"
    resp = requests.get(url, timeout=HTTP_TIMEOUT)
    if resp.status_code == 404:
        log.warning("%s has no releases yet.", repo)
        return None
    resp.raise_for_status()
    release = resp.json()

    # `releases/latest` already excludes prereleases on Forgejo, but the flag is
    # cheap to re-check and this is the thing that keeps -rc tags off phones.
    if release.get("prerelease"):
        log.info("%s: latest release %s is a prerelease — skipping.",
                 repo, release.get("tag_name"))
        return None
    return release


def fetch_metadata(repo: str, tag: str, metadata_path: str) -> int:
    """
    Replace WORK/metadata with the source repo's F-Droid metadata at `tag`.

    Uses the archive endpoint rather than a git clone: one request, no git in
    the image, and it is pinned to the exact tag being published — so the app
    listings can never describe a different release than the APKs do.
    """
    url = f"{FORGEJO}/api/v1/repos/{repo}/archive/{tag}.tar.gz"
    resp = requests.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()

    wanted = metadata_path.strip("/")
    count = 0
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            # Archive members are prefixed with a top-level directory.
            rel = member.name.split("/", 1)[-1]
            if not rel.startswith(wanted + "/") or not rel.endswith(".yml"):
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            (METADATA / Path(rel).name).write_bytes(extracted.read())
            count += 1

    if count == 0:
        raise RuntimeError(f"{repo}@{tag} has no metadata under {metadata_path}/")
    log.info("%s@%s: fetched %d metadata file(s).", repo, tag, count)
    return count


def asset_fingerprint(release: dict[str, Any], glob: str) -> list[list]:
    """
    Sorted `[name, size]` pairs for the release assets matching `glob`.

    State keys on this rather than on the tag alone, because a release can gain
    assets *after* the tag has stayed the same — e.g. a CI job that uploads
    APKs sequentially finishes the second one a few minutes after the first,
    or a maintainer attaches a missing package to an existing release by hand.
    A tag-only skip check would freeze the incomplete first snapshot in place
    forever (this exact bug shipped once: shepherd-launcher 0.3.2 was published
    without its media package because the media APK arrived on a later poll).
    """
    return sorted(
        [a["name"], a.get("size", 0)]
        for a in release.get("assets", [])
        if fnmatch.fnmatch(a["name"], glob)
    )


def download_assets(release: dict[str, Any], glob: str) -> list[str]:
    """Download matching release assets into WORK/repo. Returns their filenames."""
    names: list[str] = []
    for asset in release.get("assets", []):
        name = asset["name"]
        if not fnmatch.fnmatch(name, glob):
            continue
        dest = REPO / name
        if dest.exists() and dest.stat().st_size == asset.get("size", -1):
            log.info("  %s already present.", name)
            names.append(name)
            continue
        log.info("  downloading %s (%.1f MB)...", name, asset.get("size", 0) / 1048576)
        with requests.get(internal_url(asset["browser_download_url"]), stream=True,
                          timeout=HTTP_TIMEOUT) as resp:
            resp.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    fh.write(chunk)
            tmp.replace(dest)
        names.append(name)

    if not names:
        raise RuntimeError(f"release {release.get('tag_name')} has no assets matching {glob}")
    return names


# ── fdroid ────────────────────────────────────────────────────────────────────

def write_fdroid_config(cfg: dict[str, Any]) -> None:
    config = {
        "repo_url": cfg["repo_url"],
        "repo_name": cfg["repo_name"],
        "repo_description": cfg["repo_description"],
        # 0 = never archive: every version stays in the one repo/ directory, so
        # users can downgrade and there is no second tree to publish atomically.
        # Revisit if disk becomes tight — see the README.
        "archive_older": 0,
        "keystore": str(KEYSTORE),
        "repo_keyalias": KEYALIAS,
        "keystorepass": KEYSTOREPASS,
        "keypass": KEYPASS,
    }
    path = WORK / "config.yml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    path.chmod(0o600)   # fdroid warns about anything looser


def run_fdroid_update() -> None:
    log.info("Running fdroid update...")
    proc = subprocess.run(
        ["fdroid", "update", "--delete-unknown", "--pretty"],
        cwd=WORK, capture_output=True, text=True,
    )
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.strip():
            log.info("  %s", line.rstrip())
    if proc.returncode != 0:
        raise RuntimeError(f"fdroid update exited {proc.returncode}")


def verify_published(expected: list[str]) -> None:
    """
    Assert every APK we just downloaded actually reached the index.

    This is the security-relevant check, and it exists because a *successful*
    `fdroid update` is not evidence that anything was published. An APK signed
    by a key that the app's metadata does not list in AllowedAPKSigningKeys is
    reported as a warning, removed from repo/, left out of the index — and the
    command still exits 0:

        WARNING: Removing repo/shepherd-media_0.3.0.apk
        INFO: Creating signed index with this key (SHA256): …
        INFO: Finished

    A signed index, a success exit, and one app silently missing. Since this
    service republishes release assets with no human in the loop, and F-Droid
    installs are in-place upgrades on real devices, refusing to publish is the
    only safe response.
    """
    index = REPO / "index-v2.json"
    if not index.exists():
        raise RuntimeError("fdroid update produced no index-v2.json")

    blob = index.read_text()
    missing = [name for name in expected if name not in blob]
    if missing:
        raise RuntimeError(
            "these APKs did not reach the index: " + ", ".join(missing) +
            " — the usual cause is AllowedAPKSigningKeys in the source repo's "
            "metadata not matching the key the APK is signed with"
        )
    log.info("Verified %d APK(s) present in the index.", len(expected))


# ── Publishing ────────────────────────────────────────────────────────────────

def _snapshot_copy(src: str, dst: str) -> None:
    """
    Hardlink the APKs, copy everything else.

    An APK at a given filename is immutable once published, so hardlinking the
    payload makes a snapshot instant and free. The index files are a different
    story: `fdroid update` rewrites them **in place**, so hardlinking them would
    let the next run mutate the *live* snapshot before it has been verified —
    and a run that then fails verification would leave nginx serving an index
    that advertises apps the directory no longer contains. They are small;
    copy them.
    """
    if src.endswith(".apk"):
        os.link(src, dst)
    else:
        shutil.copy2(src, dst)


def publish(tag: str) -> None:
    """
    Swap the freshly built repo into place atomically.

    nginx is serving the previous index the entire time, so the live directory
    is never mutated: build a snapshot beside it, then move a symlink.
    """
    snapshot = SRV / f"repo-{tag}"
    if snapshot.exists():
        shutil.rmtree(snapshot)

    shutil.copytree(REPO, snapshot, copy_function=_snapshot_copy)

    tmp_link = SRV / ".repo.tmp"
    if tmp_link.is_symlink() or tmp_link.exists():
        tmp_link.unlink()
    tmp_link.symlink_to(snapshot.name)
    os.replace(tmp_link, SRV / "repo")     # atomic rename(2)
    log.info("Published %s -> %s", SRV / "repo", snapshot.name)

    snapshots = sorted(SRV.glob("repo-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in snapshots[KEEP_SNAPSHOTS:]:
        log.info("Pruning old snapshot %s", old.name)
        shutil.rmtree(old, ignore_errors=True)


# ── Main loop ─────────────────────────────────────────────────────────────────

def sync_once(cfg: dict[str, Any]) -> None:
    state = load_state()
    published: list[str] = []
    newest_tag = ""

    for source in cfg["sources"]:
        repo = source["repo"]
        metadata_path = source.get("metadata_path", "dist/fdroid/metadata")
        glob = source.get("asset_glob", "*.apk")

        release = latest_release(repo)
        if release is None:
            continue

        tag = release["tag_name"]
        fingerprint = asset_fingerprint(release, glob)

        # Skip only when both the tag AND the set of matching assets are
        # unchanged from the last publish. See asset_fingerprint() for why
        # the tag alone is not enough. A legacy string entry (from before
        # this field existed) fails the isinstance check and re-processes
        # once, which is exactly what we want on upgrade.
        prior = state.get(repo)
        if (isinstance(prior, dict)
                and prior.get("tag") == tag
                and prior.get("assets") == fingerprint):
            log.debug("%s: %s already published.", repo, tag)
            continue

        log.info("%s: publishing %s", repo, tag)
        fetch_metadata(repo, tag, metadata_path)
        published += download_assets(release, glob)
        state[repo] = {"tag": tag, "assets": fingerprint}
        newest_tag = tag

    if not published:
        return

    write_fdroid_config(cfg)
    run_fdroid_update()
    verify_published(published)
    publish(newest_tag)
    save_state(state)
    log.info("Repository updated: %s", cfg["repo_url"])


def main() -> None:
    for path in (WORK, REPO, METADATA):
        path.mkdir(parents=True, exist_ok=True)

    if not KEYSTORE.exists():
        log.error("%s not found — run ./bootstrap_fdroid_key.sh first.", KEYSTORE)
        sys.exit(1)
    if not KEYSTOREPASS or not KEYPASS:
        log.error("FDROID_KEYSTOREPASS / FDROID_KEYPASS are unset — see .env.example.")
        sys.exit(1)

    cfg = load_config()
    interval = int(cfg["poll_interval_minutes"]) * 60
    log.info("fdroid sync starting. %d source(s), polling every %d min.",
             len(cfg["sources"]), interval // 60)

    def _shutdown(signum, frame):
        log.info("Shutting down.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    while True:
        try:
            # Re-read on every pass so a config edit lands without a restart.
            cfg = load_config()
            sync_once(cfg)
        # On any failure the previous snapshot stays live and the state file is
        # not advanced, so the next poll simply retries.
        except requests.RequestException as exc:
            # Network trouble is expected and self-healing — one line, no wall
            # of traceback every poll.
            log.error("Sync failed (network): %s", exc)
        except Exception as exc:
            log.error("Sync failed: %s", exc, exc_info=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
