# Releasing a new version

Cutting a release is three edits + a tag. CI ([`ci.yml`](../.github/workflows/ci.yml))
must be green first.

## 1. Bump the version

Edit `version` in [`custom_components/sun_sale/manifest.json`](../custom_components/sun_sale/manifest.json)
per [SemVer](https://semver.org):

- **patch** (`0.1.0 → 0.1.1`) — bug fixes
- **minor** (`0.1.0 → 0.2.0`) — backward-compatible features
- **major** (`0.1.0 → 1.0.0`) — breaking changes

Pure test/doc/CI changes do **not** bump.

## 2. Update the changelog

In [`CHANGELOG.md`](../CHANGELOG.md), move the `[Unreleased]` entries under a new
`## [X.Y.Z] — YYYY-MM-DD` heading and refresh the compare/tag links at the bottom.
Keep [Keep a Changelog](https://keepachangelog.com/) sections (Added/Changed/Fixed/…).

## 3. Commit, tag, push

Tag **only** a commit whose `manifest.json` version matches the tag.

```bash
git commit -am "release: vX.Y.Z"
git tag vX.Y.Z
git push origin master --tags
```

Pushing the `v*` tag triggers [`release.yml`](../.github/workflows/release.yml),
which zips `custom_components/sun_sale/` (files at archive root) and attaches
`sun_sale.zip` to a GitHub release with auto-generated notes.

## Notes

- `hacs.json` currently sets `zip_release: false`, so HACS installs from the
  source tree, not the release zip. Flip it to `true` if/when you want HACS to
  consume the attached zip.
- The user commits and tags manually — this is the convention, not automation.
