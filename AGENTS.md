# AGENTS.md

Instructions for AI coding agents working in this repository.

## Git workflow

- Push feature/agent branches directly to `main` when the branch is a clean
  fast-forward of `origin/main` (i.e. no divergent history, tests pass).
  Example: `git push origin <branch>:main`.
- **Always delete the origin copy of the working branch immediately after
  it has been merged/pushed into `main`.** Do not leave stale branches
  (e.g. `agents/*`) lingering on origin once their content is on `main`.
  Example: `git push origin --delete <branch>`.
- Do not use GitKraken tools. Use the GitHub CLI/API (`gh`) if you need to
  interact with GitHub.
