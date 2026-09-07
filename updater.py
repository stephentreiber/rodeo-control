"""GitHub Releases-based updater for Rodeo Control.

Checks the public GitHub repo for a newer tagged release than the
version installed locally, and can download + apply that release's
packaged zip in place.

Deliberately manual/click-triggered only -- nothing in here runs on
its own. This app gets used live during broadcasts, so silently
swapping code out from under an in-progress rodeo would be far worse
than asking the operator to click a button and restart between
performances.

Update packages are expected to come from package_release.py (see
that file), which produces a flat zip -- no wrapping top-level folder
-- so extraction and copy-over logic here stays simple.
"""
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
import zipfile

GITHUB_OWNER = "stephentreiber"
GITHUB_REPO = "rodeo-control"

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
VERSION_FILE = os.path.join(APP_ROOT, "VERSION")

# Never overwritten by an update, no matter what shows up in a release
# zip. This is a last line of defense on top of these already being
# gitignored (see .gitignore) and therefore absent from any release
# zip built by package_release.py in the first place -- belt and
# suspenders, since this is live event data.
PROTECTED_NAMES = {
    "rodeo_data.db", "rodeo_data.db-journal",
    "exports", "import_cache", "backups", "saved_rodeos",
}


def get_current_version():
    """Reads the locally installed VERSION file. Returns 'unknown' if
    it's missing rather than raising -- an install predating this
    file's introduction should still be able to run and check for
    updates, it just can't report what it currently is."""
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "unknown"


def check_latest_release():
    """Hits the public GitHub API for the latest tagged release.

    Returns a dict describing what's available. On any failure to
    reach GitHub (no internet, DNS, rate limiting, etc.) returns
    {"error": ...} instead -- callers must treat that as "couldn't
    check right now", never silently treat it as "no update"."""
    current = get_current_version()
    url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "rodeo-control-updater",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, ValueError) as e:
        return {"error": str(e), "current_version": current}

    tag = data.get("tag_name", "") or ""
    latest_version = tag[1:] if tag.startswith("v") else tag

    # Only look at assets we ourselves uploaded (package_release.py's
    # output) -- GitHub's own auto-attached "Source code" zip/tarball
    # never show up in this "assets" list, only manually-uploaded
    # release files do, so this can't accidentally grab those instead.
    zip_asset = next(
        (a for a in data.get("assets", [])
         if a.get("name", "").startswith("rodeo-control-") and a.get("name", "").endswith(".zip")),
        None,
    )

    return {
        "current_version": current,
        "latest_version": latest_version,
        "tag": tag,
        "notes": data.get("body", "") or "",
        "published_at": data.get("published_at", ""),
        "download_url": zip_asset["browser_download_url"] if zip_asset else None,
        "asset_name": zip_asset["name"] if zip_asset else None,
        # Plain string comparison works here because VERSION always
        # sorts correctly as text (YYYY.MM.DDx, zero-padded, single
        # trailing letter) -- same scheme as the existing build-stamp
        # convention. Revisit if that scheme ever changes.
        "update_available": bool(latest_version) and bool(current) and latest_version > current,
    }


def apply_update(download_url):
    """Downloads the given release zip and copies its contents over
    this install. Returns {"success": True} or {"success": False,
    "error": ...}.

    Does NOT restart the app -- the calling route/UI is responsible
    for telling the operator to close and relaunch afterward, since
    the currently-running Python process already has the old code
    loaded in memory regardless of what's now on disk."""
    if not download_url:
        return {"success": False, "error": "No downloadable release file found for this release."}

    tmp_dir = tempfile.mkdtemp(prefix="rodeo_update_")
    try:
        zip_path = os.path.join(tmp_dir, "update.zip")
        req = urllib.request.Request(download_url, headers={"User-Agent": "rodeo-control-updater"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(zip_path, "wb") as out:
            shutil.copyfileobj(resp, out)

        extract_dir = os.path.join(tmp_dir, "extracted")
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)

        # Sanity check before touching the live install -- catches a
        # malformed/unexpected zip rather than half-overwriting things.
        if not os.path.exists(os.path.join(extract_dir, "app.py")):
            return {"success": False,
                    "error": "That download doesn't look like a valid Rodeo Control release (app.py missing)."}

        for name in os.listdir(extract_dir):
            if name in PROTECTED_NAMES:
                continue
            src = os.path.join(extract_dir, name)
            dst = os.path.join(APP_ROOT, name)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
