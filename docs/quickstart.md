# Quick Start

This guide gets Apex Ray installed, initialized in a target repository, and running a first review.

## Install

For a one-off run:

```bash
uvx apex-ray --help
uvx apex-ray doctor
```

For a user-level CLI install:

```bash
uv tool install apex-ray
apex-ray --version
apex-ray doctor
```

`pipx install apex-ray` is also supported if you prefer pipx for isolated Python CLI tools.

## Requirements

Apex Ray can review git diffs through a language-neutral pipeline. Enhanced analyzers currently cover TypeScript/JavaScript, Python, Go, and Dart/Flutter:

- Python 3.14+
- git
- Node.js 24+ and npm only when reviewing TypeScript or JavaScript with the bundled analyzer
- Go only when reviewing Go with the bundled analyzer
- a project-compatible Dart SDK, or Flutter SDK for Flutter projects, only
  when reviewing Dart; resolve package dependencies before review
- Codex CLI or Claude Code CLI only when the selected LLM provider is a local CLI
- an API key environment variable only when the selected LLM provider is a direct or custom API
- GitHub CLI only for historical PR replay commands

Run `apex-ray doctor` from the repository you want to review. It checks git discovery, detected languages, the built-in Python analyzer, Go, Dart/FVM/Flutter SDK selection, Node.js, and the bundled TypeScript analyzer.

### Dart and Flutter setup

Apex Ray uses the project-compatible Dart SDK selected by the repository. It
does not bundle an SDK, resolve packages, or run code generation. Before the
first review:

1. Select the same Dart or Flutter SDK version used to build the project. Use
   the checked-in FVM selection when the team uses FVM.
2. When the repository uses FVM, run `fvm flutter pub get`; otherwise run
   `flutter pub get` for Flutter or `dart pub get` for pure Dart. For an
   application with a committed lockfile in CI, add `--enforce-lockfile` to
   the matching command.
3. Run `apex-ray doctor` and confirm that `Dart analyzer available` is `true`.

Dependency resolution creates `.dart_tool/package_config.json`, which the
Analysis Server uses for `package:` imports. Resolve once at the root of a Pub
workspace. In an older repository of independent packages, resolve each
changed package that owns a `pubspec.yaml`. See [Configuration](configuration.md#dart-and-flutter-analyzer)
for SDK selection and analyzer limits.

## Initialize A Project

From a repository you want Apex Ray to review:

```bash
apex-ray init
apex-ray doctor
git status --short
```

Inspect and commit the generated setup before reviewing application changes. `apex-ray init` creates shared config and review-learning directories under `.apex-ray/`, writes `.apex-ray/.gitignore` for Apex Ray generated artifacts, and can install a pre-push gate through Lefthook or a git hook.

Generated shared files are meant to be reviewed like normal project configuration. Machine-specific settings belong in `.apex-ray/config.local.yml`, which is ignored by default.

The generated config stores telemetry, LLM cache entries, and archived report runs under a shared local-data directory for the current git clone, so linked worktrees can be deleted without losing those accumulated metrics.

When Apex Ray's generated agent instructions change in a newer package version, `apex-ray doctor`, `apex-ray review`, and `apex-ray gate pre-push` warn locally if the repository's managed `AGENTS.md` block or generated skills are outdated. Refresh only those managed artifacts with:

```bash
apex-ray init --refresh-agent-artifacts --dry-run
apex-ray init --refresh-agent-artifacts
```

## First Deterministic Review

Run a no-LLM review first. This verifies diff parsing, project discovery, analyzer coverage, context pack construction, and report rendering without provider cost:

```bash
apex-ray review \
  --worktree \
  --no-llm
```

Use `--staged` to review only staged changes, `--base main` to review `main...HEAD`, or `--diff path/to/change.diff` for a supplied unified diff.

## First LLM Review

After provider configuration is ready, run:

```bash
apex-ray review \
  --worktree \
  --llm \
  --output .apex-ray/reports/review.md \
  --json .apex-ray/reports/review.json \
  --html .apex-ray/reports/review.html \
  --sarif .apex-ray/reports/review.sarif
```

The Markdown report is easiest to read locally, JSON is the durable machine-readable artifact, HTML is useful when sharing a run with teammates, and SARIF integrates with code-scanning systems.

If your shared config enables an LLM provider but a machine should stay offline or avoid provider cost, put this in `.apex-ray/config.local.yml`:

```yaml
review:
  llm:
    enabled: false
```

## Pre-Push Gate

If `apex-ray init` configured a hook, make sure the installed `apex-ray` command is available on `PATH` for git hooks. With Lefthook, activate local hooks with:

```bash
lefthook install
```

Run the hook-equivalent gate manually:

```bash
apex-ray gate pre-push
```

The gate reviews `review.base...HEAD`, writes `.apex-ray/reports/pre-push.md` and `.apex-ray/reports/pre-push.json`, and exits non-zero when the configured policy blocks the push.

## Continue Partial Coverage

Large diffs can exceed the configured LLM coverage budget. Reports include reviewed and unreviewed pack IDs plus continuation commands.

Read findings as results for the reviewed scope. `complete` means every scoped
pack and matching reviewer assignment was reviewed; `partial` means work
remains without a hard failure; `incomplete` means an over-budget pack, a
reviewer execution/verification failure, or required-reviewer policy debt
prevented completion. Zero findings in a partial or incomplete report is not a
whole-diff clean result.

Continue the highest-priority unreviewed work:

```bash
apex-ray review \
  --continue-from .apex-ray/reports/review.json \
  --residual-priority p0 \
  --llm
```

Review one specific skipped pack:

```bash
apex-ray review \
  --continue-from .apex-ray/reports/review.json \
  --only-pack "<pack-id>" \
  --llm
```

Drain an explicit baseline reviewer in bounded batches, and exit non-zero if
the limits are reached before completion:

```bash
apex-ray review \
  --continue-from .apex-ray/reports/review.json \
  --reviewer correctness \
  --until-complete \
  --strict-coverage \
  --followup-max-pack-reviews 16 \
  --max-followup-passes 8 \
  --llm
```

Use an existing reviewer id. With multiple configured reviewers and no
explicit `--reviewer`, exactly one must be marked `required: true` to act as
the baseline; on a fresh review, only that baseline reviewer runs. Explicit
completion covers only packs matching the selected reviewer, so it can finish
while unrelated global coverage remains partial. Use an unfiltered baseline
for a full-diff requirement. `exhaustive` mode still obeys normal budgets, so
it does not replace this completion contract. Markdown and JSON latest reports
are atomically updated after every completed batch and can be used to resume an
interrupted loop.

New reports also fingerprint their review input. Continuation rejects a base,
staged, or worktree report when its saved Git target or diff changed. Supplied
`--diff` reports are the only detached immutable snapshots and print a notice;
incremental pre-push Git ranges remain live, validated targets. Reports from
older Apex Ray versions have no snapshot: ordinary continuation remains
allowed with a warning, but `--until-complete` and `--strict-coverage` reject
them and require a fresh review.

## Next Steps

- Configure shared review policy in [Configuration](configuration.md).
- Configure a local CLI, direct API, or custom compatible endpoint in [LLM Providers](providers.md).
- Run focused reviewer matrices on pull requests with [GitHub Actions](github-actions.md).
- Learn how to read reports and choose review targets in [Review Workflow](review-workflow.md).
- Add project-specific rules and memory in [Rules And Memory](memory.md).
