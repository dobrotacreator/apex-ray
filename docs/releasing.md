# Releasing

This document is the maintainer runbook for Apex Ray releases.

## Overview

Release Please owns version bumps, changelog updates, tags, and GitHub Releases. It reads Conventional Commits on `main` and opens a release PR that updates:

- `pyproject.toml`
- `CHANGELOG.md`
- `.release-please-manifest.json`

Do not manually bump the package version for normal releases.

## Versioning

The Release Please manifest starts at `0.0.0` intentionally because Apex Ray has not had a published PyPI release yet. The first Release Please PR should cut `v0.1.0`; after that, the manifest records the latest released version.

Pre-1.0 versioning is conservative:

- breaking changes bump the minor version;
- features and fixes bump the patch version.

Use Conventional Commits consistently so Release Please can classify changes correctly:

```text
feat(gate): add pre-push review gate
fix(config): preserve local override precedence
refactor!: change review report schema
```

## Flow

1. Merge ordinary feature and fix PRs into `main`.
2. The `Release Please` workflow opens or updates a release PR.
3. Review the generated version, changelog, and manifest changes.
4. Merge the release PR when ready.
5. Release Please creates the GitHub Release and tag.
6. The `Publish PyPI` workflow builds from that release tag.
7. Approve the `pypi` environment deployment if required.
8. Confirm the package page and smoke install:

```bash
uvx apex-ray --version
uvx apex-ray doctor
```

## First PyPI Setup

Before the first publish, configure PyPI Trusted Publishing for a pending project named `apex-ray`.

PyPI trusted publisher values:

- project name: `apex-ray`
- owner: `dobrotacreator`
- repository: `apex-ray`
- workflow filename: `publish-pypi.yml`
- environment: `pypi`

GitHub repository setup:

- create an environment named `pypi`;
- require maintainer approval for deployments to `pypi`;
- allow GitHub Actions to create pull requests;
- allow workflow write permissions for the Release Please workflow.

The publish workflow uses OIDC through `pypa/gh-action-pypi-publish`; do not add a long-lived PyPI API token unless Trusted Publishing is unavailable.

## Manual Checks

The release workflow builds and checks distributions from the release tag. For local verification before merging a release PR, run:

```bash
npm --prefix analyzers/typescript ci
npm --prefix analyzers/typescript run build
uv sync --locked --all-groups
uv build --sdist --wheel
uv run twine check dist/*
```
