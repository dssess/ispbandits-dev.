# Preparing ISPbandits Dev for Git and GitHub — Historical Guide

> **Historical status:** This document was written before the first GitHub publication. The owner subsequently published `dssess/ispbandits-dev.` as a public repository. Do not follow the repository-creation or visibility steps below as if they describe the current state. Public visibility does not change the proprietary license.

## Current project state

The project has an existing `main` branch and a GitHub remote at `https://github.com/dssess/ispbandits-dev.`. Repository visibility was changed to public by owner request. The alpha source is not production-validated; check the live repository for its current commits and CI state. Historical archive instructions below apply only if preparing a separate copy from an old archive.

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

## 4. Historical: create the initial private repository (already completed)

This section records the original private-first plan only. The repository already exists and is public by owner request. Do not create a duplicate repository or change visibility based on this historical guidance. For a separate new repository, the original private-first approach is still safer for proprietary software.

Then add the repository using SSH (recommended) and push the `main` branch:

```bash
git remote add origin git@github.com:OWNER/ispbandits-dev.git
git remote -v
git push -u origin main
```

Replace `OWNER` with the GitHub organization or username. Configure SSH authentication or sign in with GitHub CLI beforehand. Do **not** put a GitHub token/password in a remote URL, shell history, project file, or chat. If the current working copy is only the archive extraction, make the initial commit first, then add the remote and push.

## 5. Historical post-push checklist

The initial push and public visibility change are complete. Keep releases marked as alpha until the full installer and experimental SSH meter have been exercised on disposable Ubuntu 22.04/24.04 and Debian targets, and traffic accounting, quota behavior, backups, and client connectivity are verified.

Before making the repository public, obtain explicit owner approval, review the proprietary terms, scan the entire Git history for secrets, and decide which security and issue-reporting information may be public. The copyright notice signals ownership; it does not technically prevent copying.
