# Changelog

## 0.1.0 (2026-05-31)


### ⚠ BREAKING CHANGES

* prepare Apex Ray for production readiness ([#10](https://github.com/dobrotacreator/apex-ray/issues/10))
* reset the pre-release public history to a sanitized production baseline before wider use.

### Features

* release Apex Ray local review engine ([1105a4f](https://github.com/dobrotacreator/apex-ray/commit/1105a4f444c0399058af723fb911993f43d6b34a))


### Refactoring

* prepare Apex Ray for production readiness ([#10](https://github.com/dobrotacreator/apex-ray/issues/10)) ([6c7cde6](https://github.com/dobrotacreator/apex-ray/commit/6c7cde6b85b6a2e3b8ad699dc2f5e35dadb84e99))


### Documentation

* add animated project logo ([2911320](https://github.com/dobrotacreator/apex-ray/commit/291132084b849851589429d7bc5da881b0034581))
* refresh logo assets ([#9](https://github.com/dobrotacreator/apex-ray/issues/9)) ([98932a5](https://github.com/dobrotacreator/apex-ray/commit/98932a5b25d24a99a7ac02328b67ba5e79123a78))


### Infrastructure

* **release:** add Release Please automation ([#11](https://github.com/dobrotacreator/apex-ray/issues/11)) ([8361715](https://github.com/dobrotacreator/apex-ray/commit/8361715de97654b446d7e693ec0954167fc036ee))

## Changelog

All notable changes to Apex Ray will be documented here.

The format follows Keep a Changelog style categories and uses Conventional Commits for commit history.

## Unreleased

### Added

- Local TS/JS code-review reports with optional LLM review and verifier passes.
- Context-pack coverage summaries, continuation flows, and local review telemetry.
- Historical PR replay evaluation utilities for comparing Apex Ray against prior review comments.

### Changed

- Pre-1.0 report schemas may change while the tool is prepared for production use.
