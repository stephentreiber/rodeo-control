"""Builds a release-ready zip for Rodeo Control.

Run from anywhere inside the repo:

    python scripts/package_release.py              # work-in-progress build
    python scripts/package_release.py --committed   # official, release-ready build

Packages every git-tracked file (via `git ls-files`, so anything in
.gitignore -- the database, exports, import cache, backups, saved
rodeos -- is automatically excluded, no separate exclude-list to keep
in sync) into a single flat zip: extracting it drops app.py,
static/, templates/, etc. straight into whatever folder is extracted
into, with no extra wrapping folder to navigate through.

Work-in-progress builds (the default, no flag) are named from VERSION
with an "-uncommitted" suffix, e.g. rodeo-control-2026.09.06a-uncommitted.zip
-- useful for telling local handoff zips apart on disk, and a visual
reminder not to attach one to a GitHub Release.

--committed builds are deliberately named just rodeo-control.zip, with
NO version in the filename. This is what should get attached as the
GitHub Release asset. Reasoning: when someone extracts a zip via
Explorer/Finder's default "Extract All", the OS creates a folder named
after the zip file -- if that name has a version baked in, it goes
stale the moment the in-app updater (see updater.py) later updates the
files without renaming the folder. A fixed, version-free asset name
means every future first-time install lands in a folder called
"rodeo-control" permanently, with no reliance on anyone renaming
anything by hand. The version itself still lives in the GitHub
Release's tag/title, and inside the running app (Settings page and the
startup console banner).
"""
import os
import subprocess
import sys
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION_FILE = os.path.join(REPO_ROOT, "VERSION")
OUTPUT_DIR = os.path.join(REPO_ROOT, "dist")


def _tracked_files():
    result = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def main():
    committed = "--committed" in sys.argv

    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            version = f.read().strip()
    except FileNotFoundError:
        print(f"No VERSION file found at {VERSION_FILE} -- create one first (e.g. '2026.09.06a').")
        sys.exit(1)

    if not version:
        print("VERSION file is empty -- put a version string in it first.")
        sys.exit(1)

    try:
        files = _tracked_files()
    except subprocess.CalledProcessError:
        print("`git ls-files` failed -- run this from inside the repo (a git checkout).")
        sys.exit(1)

    if not files:
        print("`git ls-files` returned nothing -- run this from inside the repo.")
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    zip_name = "rodeo-control.zip" if committed else f"rodeo-control-{version}-uncommitted.zip"
    zip_path = os.path.join(OUTPUT_DIR, zip_name)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel_path in files:
            full_path = os.path.join(REPO_ROOT, rel_path)
            # arcname = rel_path itself (e.g. "static/style.css"), not
            # prefixed with any folder -- this is what keeps the zip
            # flat with no wrapping top-level directory.
            zf.write(full_path, arcname=rel_path)

    print(f"Built {zip_path} (version {version}, {len(files)} files).")
    if committed:
        print("Attach this file as-is (don't rename it) as a Release asset on GitHub.")
        print("The fixed filename is what keeps future first-time installs from getting")
        print("a stale version baked into their extracted folder name.")
    else:
        print("This is a work-in-progress build -- do NOT attach it to a GitHub Release.")
        print("Once this code is actually committed (and tagged, for an official release),")
        print("re-run with --committed to get the release-ready build.")


if __name__ == "__main__":
    main()
