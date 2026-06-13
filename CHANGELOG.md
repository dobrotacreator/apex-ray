# Changelog

## [0.1.9](https://github.com/dobrotacreator/apex-ray/compare/v0.1.8...v0.1.9) (2026-06-13)


### Features

* add local finding triage ([#53](https://github.com/dobrotacreator/apex-ray/issues/53)) ([f55c5f9](https://github.com/dobrotacreator/apex-ray/commit/f55c5f93b2bd430bf4d0608f299be0257ba82d75))
* add semantic go analyzer ([#55](https://github.com/dobrotacreator/apex-ray/issues/55)) ([fc6d5af](https://github.com/dobrotacreator/apex-ray/commit/fc6d5af8cb079ad36813bc6212b4eb8cba15adc1))
* **gate:** support rule resolution surfaces ([#58](https://github.com/dobrotacreator/apex-ray/issues/58)) ([2b330f8](https://github.com/dobrotacreator/apex-ray/commit/2b330f8658d05f94dce551c72346255c39c0df27))
* refresh generated agent artifacts ([#54](https://github.com/dobrotacreator/apex-ray/issues/54)) ([a974efc](https://github.com/dobrotacreator/apex-ray/commit/a974efc64265771a595f28a3c161af785683a2b5))


### Documentation

* clarify apex-ray pre-push ownership ([#51](https://github.com/dobrotacreator/apex-ray/issues/51)) ([82983b3](https://github.com/dobrotacreator/apex-ray/commit/82983b3638b9b889424de7713eae95250971194b))
* update Go analyzer coverage ([#59](https://github.com/dobrotacreator/apex-ray/issues/59)) ([df0a951](https://github.com/dobrotacreator/apex-ray/commit/df0a951e7884e2d38bf9092e5d0c75a68e5b3a57))

## [0.1.8](https://github.com/dobrotacreator/apex-ray/compare/v0.1.7...v0.1.8) (2026-06-07)


### Features

* persist local data across worktrees ([#49](https://github.com/dobrotacreator/apex-ray/issues/49)) ([e031fe0](https://github.com/dobrotacreator/apex-ray/commit/e031fe08f5e1c213b05ff416badd48c837d8664e))


### Bug Fixes

* drop stale carried gate findings ([#45](https://github.com/dobrotacreator/apex-ray/issues/45)) ([71a9ed2](https://github.com/dobrotacreator/apex-ray/commit/71a9ed2878aa57db0948728ac29607a51ff4b013))
* harden TypeScript analyzer timeouts ([#48](https://github.com/dobrotacreator/apex-ray/issues/48)) ([3ac897e](https://github.com/dobrotacreator/apex-ray/commit/3ac897e98364a7b4576406584feb9d6a712b1cbd))

## [0.1.7](https://github.com/dobrotacreator/apex-ray/compare/v0.1.6...v0.1.7) (2026-06-06)


### Bug Fixes

* load legacy review reports for continuation ([#43](https://github.com/dobrotacreator/apex-ray/issues/43)) ([da3fa61](https://github.com/dobrotacreator/apex-ray/commit/da3fa6118d1284b9411027d8fa9e561e816d011f))

## [0.1.6](https://github.com/dobrotacreator/apex-ray/compare/v0.1.5...v0.1.6) (2026-06-05)


### Features

* add analyzer orchestration foundation ([#26](https://github.com/dobrotacreator/apex-ray/issues/26)) ([de57e32](https://github.com/dobrotacreator/apex-ray/commit/de57e32b57e4e9ce6dee8b337c1aebe47dce97ef))
* add Python analyzer quality benchmarks ([#38](https://github.com/dobrotacreator/apex-ray/issues/38)) ([1b99b25](https://github.com/dobrotacreator/apex-ray/commit/1b99b25fa56bd7f4511ea8345cecd4895ef5eee2))
* add Python structural analyzer ([#28](https://github.com/dobrotacreator/apex-ray/issues/28)) ([7a9a9af](https://github.com/dobrotacreator/apex-ray/commit/7a9a9af12ef7f019242cdd5a1485ebc628a76c30))
* add Python-aware review prompts ([#37](https://github.com/dobrotacreator/apex-ray/issues/37)) ([2e6869b](https://github.com/dobrotacreator/apex-ray/commit/2e6869bd5960417853213784e4f4e5086e83a516))
* enrich Python analyzer reference context ([#30](https://github.com/dobrotacreator/apex-ray/issues/30)) ([2246925](https://github.com/dobrotacreator/apex-ray/commit/2246925b3e25c536f7406fb26d259f5cdf39ab92))
* harden Python boundary analysis ([#40](https://github.com/dobrotacreator/apex-ray/issues/40)) ([3ac998a](https://github.com/dobrotacreator/apex-ray/commit/3ac998a7b2d29bbc84fec3244c624dd3d354f901))


### Bug Fixes

* handle Claude structured output in gate reviews ([#29](https://github.com/dobrotacreator/apex-ray/issues/29)) ([7ae551d](https://github.com/dobrotacreator/apex-ray/commit/7ae551d892a7827f5b3e7ab54f2a2b2c9325dffe))
* scale TypeScript analyzer timeout for large diffs ([#42](https://github.com/dobrotacreator/apex-ray/issues/42)) ([233c1c3](https://github.com/dobrotacreator/apex-ray/commit/233c1c369f30ee9646d1a852e89632142782ca6f))


### Refactoring

* reorganize analyzer layout ([#31](https://github.com/dobrotacreator/apex-ray/issues/31)) ([2e41d42](https://github.com/dobrotacreator/apex-ray/commit/2e41d429f566900648d109611000e1520586b0bc))
* split Python analyzer package ([#39](https://github.com/dobrotacreator/apex-ray/issues/39)) ([b88cd87](https://github.com/dobrotacreator/apex-ray/commit/b88cd877056e4b4e856958ca87900bf7419c8fd8))


### Documentation

* productize Python support ([#41](https://github.com/dobrotacreator/apex-ray/issues/41)) ([4aa7fdd](https://github.com/dobrotacreator/apex-ray/commit/4aa7fddb91106749ce55d46809405d9aad0be70d))

## [0.1.5](https://github.com/dobrotacreator/apex-ray/compare/v0.1.4...v0.1.5) (2026-06-02)


### Features

* **gate:** add strict incremental pre-push retry ([#25](https://github.com/dobrotacreator/apex-ray/issues/25)) ([2cd1c19](https://github.com/dobrotacreator/apex-ray/commit/2cd1c1940bed386008800ba487171a20928ece0a))


### Documentation

* add MkDocs documentation site ([#22](https://github.com/dobrotacreator/apex-ray/issues/22)) ([0eabc74](https://github.com/dobrotacreator/apex-ray/commit/0eabc7404e56dc9d93b8da73fd2ef29869806677))
* polish documentation site content ([#24](https://github.com/dobrotacreator/apex-ray/issues/24)) ([66f8689](https://github.com/dobrotacreator/apex-ray/commit/66f868981b995f1d9bad8788cf1d79faf55b2df2))

## [0.1.4](https://github.com/dobrotacreator/apex-ray/compare/v0.1.3...v0.1.4) (2026-06-01)


### Features

* **gate:** show pre-push progress output ([#20](https://github.com/dobrotacreator/apex-ray/issues/20)) ([8040045](https://github.com/dobrotacreator/apex-ray/commit/804004514189098f0b0d34083979ad0141f2140a))

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
