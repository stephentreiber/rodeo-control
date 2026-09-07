# Cutting a release

Steps to publish a new version so the in-app updater (Settings → Updates)
can find and install it.

## Work-in-progress builds

Running `python scripts/package_release.py` with no flags at any point
produces `dist/rodeo-control-<VERSION>-uncommitted.zip` -- the
"-uncommitted" suffix is a deliberate visual reminder that this zip's
contents may not match anything actually committed to git yet, so it
should never be attached to a GitHub Release. This is what a normal
work session's handoff zip looks like; `VERSION` does not need to change
between these.

## Publishing an actual release

1. **Bump the version.** Edit `VERSION` at the repo root to the new version
   string, following the existing date-based convention:
   `YYYY.MM.DD` + a trailing letter for same-day builds (e.g. `2026.09.06a`,
   then `2026.09.06b` for a second build that same day, `2026.09.07a` the
   next day, and so on). This is a plain text file — one line, no `v`
   prefix inside it. Only do this once you're actually ready to commit --
   not for every work-in-progress zip along the way.

2. **Commit and push.**
   ```
   git add -A
   git commit -m "Bump version to 2026.09.06a"
   git push
   ```

3. **Tag the release.** The tag name gets a `v` prefix (the `VERSION` file
   itself does not):
   ```
   git tag v2026.09.06a
   git push origin v2026.09.06a
   ```

4. **Build the release zip, with `--committed`.** Run this *after* the
   version bump is committed — the packaging script only includes
   git-tracked files (`git ls-files`), so anything not yet committed won't
   be in the zip:
   ```
   python scripts/package_release.py --committed
   ```
   This produces `dist/rodeo-control.zip` — deliberately with **no
   version in the filename**. Every release re-uses this exact same
   asset name; the version lives in the git tag, the release title, and
   inside the running app instead. The reason: when someone extracts a
   zip via Explorer/Finder's default "Extract All", the OS names the
   resulting folder after the zip file. If that name has a version baked
   in, it goes stale the moment a later in-app update changes the files
   without renaming the folder. A fixed asset name means every future
   first-time install lands in a folder called `rodeo-control`
   permanently, with nothing for anyone to rename by hand. It's also a
   flat zip (no wrapping folder) containing everything tracked in git,
   which automatically excludes the database, exports, caches, and
   backups since those are already in `.gitignore`.

5. **Create the GitHub Release.**
   - Go to the repo's **Releases** page → **Draft a new release**.
   - Choose the tag pushed in step 3 (`v2026.09.06a`).
   - Give it a title (the version string is fine) and write release notes
     describing what changed — these notes are shown directly in the
     app's "Check for Updates" screen, so keep them in plain, generic
     language (no names — anyone using this app should be able to read
     them and understand what's new).
   - Under **Assets**, attach the zip built in step 4 (`rodeo-control.zip`,
     unrenamed). Every release attaches a file with this exact same name
     — that's expected and intentional, not a mistake.
   - Click **Publish release**.

6. **Verify.** Open the app, go to **Settings → Updates**, and click
   **Check for Updates**. It should show the new version with the release
   notes and a working **Download & Install Update** button. Applying it
   on a test copy first (not a machine mid-event) is a reasonable sanity
   check before relying on it live.

## Notes

- The updater only ever looks at the **latest** published release — draft
  and pre-release entries are ignored by GitHub's `releases/latest` API
  endpoint, so it's safe to draft one ahead of time without it being
  offered to anyone yet.
- If step 4 is run before step 2/3 (version bump not yet committed), the
  zip will silently contain the *old* `VERSION` file. If the "Check for
  Updates" screen ever shows the wrong version after a release, this is
  the first thing to check.
- Skipping the zip-asset upload (step 5) still creates a valid GitHub
  Release, but the in-app updater has nothing to download — it looks
  specifically for an attached asset named exactly `rodeo-control.zip`,
  not GitHub's automatic "Source code" zip/tarball, and not any
  work-in-progress build's "-uncommitted" filename either.
- An install folder from before this change may still have a stale
  version baked into its name — that's harmless and safe to rename
  any time by hand, since the app doesn't care what its own folder is
  called. Only *new* first-time installs going forward automatically
  avoid the problem.
