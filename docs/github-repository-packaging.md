# Preparing ISPbandits Dev for Git and GitHub

This guide covers preparing the **ISPbandits Dev v1 alpha** source for future GitHub deployment. It does not create a GitHub repository, publish code, or deploy to a VPS.

## Current project state

The current workspace is already a Git repository on branch `main`. Its source files are staged for an initial commit, but there is **no commit and no remote**. The downloadable source archive `/home/ubuntu/ispbandits-dev-v1-alpha.tar.gz` contains the project files but intentionally excludes `.git` history. When the archive is shared, verify its SHA-256 against the checksum supplied alongside it.

If continuing from the current checkout, do not run `git init` again. If starting from the archive on another computer, initialize a new repository after extraction.

## 1. Prepare the source copy

Transfer/download the archive, then verify its hash before extracting. Replace the sample archive path with where it was saved:

```bash
sha256sum ispbandits-dev-v1-alpha.tar.gz
mkdir ispbandits-dev
tar -xzf ispbandits-dev-v1-alpha.tar.gz -C ispbandits-dev
cd ispbandits-dev
```

Compare the displayed SHA-256 with the value supplied with the archive. The archive includes source, tests, installer, systemd units, and documentation; it does not include Git history, a deployed database, customer accounts, backups, or VPS credentials.

## 2. Verify before staging or committing

Run the same checks used for the alpha before committing:

```bash
bash -n install.sh scripts/refresh-tls.sh
PYTHONPATH=src python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover -s tests -v
./install.sh --check
```

Inspect the files and staged contents. In particular, verify the ignore rules and check for credentials, deployment certificates, private keys, databases, and backups:

```bash
cat .gitignore
git status --short
git diff --check
git diff --cached --check
```

The project `.gitignore` excludes SQLite database files, backup archives, `.env` files, Python caches, certificates/private keys, local Xray configuration, logs, and build outputs. Never add a real VPS database, backup archive, TLS private key, SSH private key, password, token, or generated secret-bearing `xray.json` file.

If a secret was ever committed, removing it in a later commit is not enough: revoke/rotate it and clean the repository history before publishing.

## 3. Create the initial local repository (archive workflow only)

The archive has no `.git` directory. From its extracted project directory, run:

```bash
git init -b main
git add -A
git diff --cached --check
git status --short
git diff --cached --stat
```

Review the status and complete staged diff before creating a commit. The `LICENSE.proprietary` file is intentional; do not replace it with an open-source license unless the owner explicitly changes the licensing decision. Set Git author identity to the correct person/company account using your normal Git configuration; do not claim someone else's identity.

```bash
git config user.name "Your Name"
git config user.email "you@example.com"
git commit -m "Initial ISPbandits Dev v1 alpha"
```

If using the existing checkout instead, skip `git init` and `git add` only after reviewing the already-staged changes:

```bash
cd /path/to/ispbandits-dev
git status --short
git diff --cached --check
git diff --cached --stat
```

## 4. Create a private GitHub repository later

Keep the repository **private** while the software is proprietary and before release review. Create a blank private repository named `ispbandits-dev` under the correct ISPbandits Dev owner/account. Do not initialize it with a second README, license, or `.gitignore` if you plan to push this existing project history.

Then add the repository using SSH (recommended) and push the `main` branch:

```bash
git remote add origin git@github.com:OWNER/ispbandits-dev.git
git remote -v
git push -u origin main
```

Replace `OWNER` with the GitHub organization or username. Configure SSH authentication or sign in with GitHub CLI beforehand. Do **not** put a GitHub token/password in a remote URL, shell history, project file, or chat. If the current working copy is only the archive extraction, make the initial commit first, then add the remote and push.

## 5. After the first push

Confirm the GitHub repository is private, the expected branch and workflow files are present, and GitHub Actions runs the test workflow successfully. Enable branch protection for `main` before accepting outside contributions. Keep releases marked as alpha until the full installer has been exercised on disposable Ubuntu 22.04/24.04 and Debian targets and the remaining metering, backup, and connectivity work is verified.

Before making the repository public, obtain explicit owner approval, review the proprietary terms, scan the entire Git history for secrets, and decide which security and issue-reporting information may be public. The copyright notice signals ownership; it does not technically prevent copying.
