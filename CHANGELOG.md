# Changelog

## [0.1.3](https://github.com/dobrotacreator/apex-ray/compare/v0.1.2...v0.1.3) (2026-06-01)


### Bug Fixes

* **release:** update uv lock from release please ([#18](https://github.com/dobrotacreator/apex-ray/issues/18)) ([bc061e6](https://github.com/dobrotacreator/apex-ray/commit/bc061e63c62b29aa6d3a0d44888935a3f39f6bb8))

## [0.1.2](https://github.com/dobrotacreator/apex-ray/compare/v0.1.1...v0.1.2) (2026-06-01)


### Bug Fixes

* **release:** keep uv lock version in sync ([#16](https://github.com/dobrotacreator/apex-ray/issues/16)) ([8907f4a](https://github.com/dobrotacreator/apex-ray/commit/8907f4a9a576d595a203219f062b497e6d47b491))

## [0.1.1](https://github.com/dobrotacreator/apex-ray/compare/v0.1.0...v0.1.1) (2026-06-01)


### Features

* **telemetry:** record llm usage and archive reports ([#15](https://github.com/dobrotacreator/apex-ray/issues/15)) ([1f1be19](https://github.com/dobrotacreator/apex-ray/commit/1f1be19cce9bb75d54b7c001bbb20b35f191b7fa))


### Infrastructure

* **release:** allow manual PyPI publish ([#13](https://github.com/dobrotacreator/apex-ray/issues/13)) ([f946516](https://github.com/dobrotacreator/apex-ray/commit/f9465167fc682a3ec29b1654830935d5dffc57ad))

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
