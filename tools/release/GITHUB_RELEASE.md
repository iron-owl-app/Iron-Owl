# GitHub releases: one-time setup

`.github/workflows/release.yml` signs the update file in its own job, `sign`, which uses the
GitHub Environment named `release`. Set it up once, before the first `v*` tag. Do these on
github.com, in the repository's **Settings**.

## 1. The signing key

1. Make the key on your own PC: `python tools/release/keygen.py --key-id io-2026a`
   (pick a good passphrase; keep the `.pem` file and the passphrase safe and offline).
2. Paste the printed public key line into `GITHUB_KEYS` in `backend/app/update_keys.py`,
   add it to `tools/public/allow.txt`, and commit.

## 2. The `release` environment

1. **Settings > Environments > New environment**, name it `release`.
2. **Required reviewers**: add yourself (and only people you trust to publish). Every signing
   run then waits until a reviewer approves it.
3. **Deployment branches and tags**: choose **Selected branches and tags**, add a **tag** rule
   `v*`. Nothing else may use this environment.
4. **Environment secrets**: add
   - `IRON_OWL_SIGNING_KEY`: the whole text of the `.pem` file;
   - `IRON_OWL_SIGNING_PASSPHRASE`: its passphrase.
5. **Settings > Secrets and variables > Actions > Repository secrets**: make sure neither secret
   is there. If it is, delete it: repository secrets can be read by any workflow anyone with
   write access pushes.

## 3. Who may make release tags

1. **Settings > Rules > Rulesets > New ruleset > New tag ruleset**, name it `Release tags`,
   enforcement **Active**.
2. **Target tags**: include by pattern `v*`.
3. Tick **Restrict creations**, **Restrict updates** and **Restrict deletions**.
4. **Bypass list**: add only yourself (Repository admin role, or your account).

## Every release

1. Bump `backend/app/version.py`, add a `## X.Y.Z` section to `RELEASE_NOTES.md`, commit.
2. Push the tag `vX.Y.Z`. The `build` job runs; then the `sign` job waits for your approval
   (**Actions > the run > Review deployments > release > Approve**).
3. Check the draft release (the `.exe`, the `.ftupdate`, `SHA256SUMS.txt`), test the `.exe`
   in Windows Sandbox, then publish it.
