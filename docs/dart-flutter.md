# Dart And Flutter Review

Apex Ray uses the Dart Analysis Server included in the SDK selected by the
reviewed project. It does not bundle Dart or Flutter, download an SDK, run
code generation, or resolve packages. This keeps the analyzer aligned with the
project's language version and makes local and CI behavior reproducible.

## Prerequisites

Before review:

1. Install the Dart SDK, or install Flutter when the repository contains a
   Flutter application. Flutter includes a complete Dart SDK.
2. Select the same SDK version used to build the application. A checked-in FVM
   selection is preferred when the team uses FVM.
3. Resolve dependencies with `flutter pub get` for Flutter packages or
   `dart pub get` for pure Dart packages. In CI for an application with a
   committed lockfile, prefer `flutter pub get --enforce-lockfile`.
4. Run `apex-ray doctor` from the repository root and confirm that Dart is
   detected and `Dart analyzer available` is `true`.

Dependency resolution creates `.dart_tool/package_config.json`, which the
language server uses for `package:` imports. Keep `.dart_tool/` uncommitted.
For a Pub workspace, resolve once at the workspace root. In an older
multi-package repository without a Pub workspace, resolve every changed local
package that has its own `pubspec.yaml`.

## SDK Selection

Apex Ray resolves the Dart command in this order:

1. `review.analyzer.dart.command`;
2. `.fvm/flutter_sdk/bin/dart` in the repository;
3. `dart` on `PATH`;
4. `fvm dart` when FVM is on `PATH`;
5. the Dart executable next to the resolved `flutter` executable when that
   location is unambiguous.

The configured command is a YAML argument list, not a shell command:

```yaml
review:
  analyzer:
    dart:
      command:
        - /opt/flutter/bin/dart
```

For FVM, run commands in this order so dependency resolution and Apex Ray use
the same SDK:

```bash
fvm flutter pub get
apex-ray doctor
apex-ray review --worktree --no-llm
```

Normally no explicit command is needed: FVM's project-local SDK is detected
before `PATH`. If the repository uses a custom SDK wrapper, configure every
argument separately, for example `command: [fvm, dart]`. Apex Ray appends the
`language-server` arguments and never evaluates the command through a shell.

## Configuration

The defaults are deliberately bounded and work for ordinary Dart and Flutter
diffs:

```yaml
review:
  analyzer:
    timeout_seconds: 120
    index_cache_enabled: true
    dart:
      enabled: true
      command: []
      flutter: auto
      plugins: true
      max_changed_symbols: 80
      max_references_per_symbol: 24
      max_callees_per_symbol: 16
      max_related_tests_per_file: 12
      max_dependency_package_anchors: 16
```

`flutter` accepts:

- `auto`: enable Flutter outline/context when a Flutter manifest or import is
  present;
- `enabled`: request Flutter context explicitly;
- `disabled`: run Dart semantics without Flutter outline enrichment.

`plugins` controls Dart analyzer plugins. It defaults to `true` so local,
trusted reviews match the project's normal analyzer behavior. Analyzer plugins
are executable code loaded by the Dart language server, so review an untrusted
checkout only with `plugins: false` and a Dart SDK supplied outside that
checkout. SDKs that do not support disabling plugins cannot provide this
fail-closed semantic mode; the bundled GitHub Action then disables only the
Dart backend and retains diff-only review coverage.

The symbol, reference, callee, and test limits cap semantic fan-out before
context-pack budgeting. `max_dependency_package_anchors` bounds how many
reverse-dependent local packages Apex Ray opens to discover cross-package
consumers. This includes both path dependencies and version dependencies
between members of a modern Pub workspace. Lower the limits for very broad
mechanical diffs; raise them only after measuring analyzer time, prompt size,
and missed relationships. The global analyzer timeout is a hard deadline for
the Dart server and all semantic requests.

Disable only the Dart backend without changing other languages:

```yaml
review:
  analyzer:
    dart:
      enabled: false
```

## Semantic Coverage

For changed handwritten declarations, Apex Ray collects bounded repository
context from the language server:

- nested classes, mixins, extensions, extension types, enums, constructors,
  methods, accessors, fields, top-level functions and variables, and typedefs;
- exact declaration ranges and Dart library privacy for `_private` names;
- repository references, incoming and outgoing calls, and available type
  hierarchy contracts;
- imports, exports, `part`, and `part of` relationships;
- related unit, widget, golden, and integration tests, ranking semantic test
  references ahead of file-name conventions;
- diagnostics as context metadata, not automatic Apex Ray findings.

Flutter-aware metadata activates only from concrete imports, declarations,
annotations, or call sites. It covers Widget/State and `createState`
relationships, lifecycle methods and resource cleanup, async `BuildContext`
use, BLoC/Cubit, MobX, provider/service-locator access, GoRouter and Navigator,
serialization annotations, permissions, storage, networking, isolates, and
background work. Exact literal MethodChannel, EventChannel, and
BasicMessageChannel names can be connected to matching Kotlin, Java, Swift,
or Objective-C handlers in the repository.

This is evidence for review, not a framework emulator. Runtime-generated
routes, dynamic channel names, reflection-like registration, custom wrappers,
and relationships hidden behind unresolved dependencies can remain unknown.
Apex Ray reports partial coverage instead of inventing links.

## Generated Code

Common generated outputs are classified before test/source handling:

- `*.g.dart`
- `*.freezed.dart`
- `*.config.dart`
- `*.mocks.dart`
- `*.gr.dart`
- `*.chopper.dart`

These files remain visible to the Dart server for resolution, but Apex Ray
does not review them as changed targets and does not copy their source into
changed, reference, contract, metadata, or related-test snippets. A generated
reference may contribute a bounded suppression count while the handwritten
annotation, declaration, `part` directive, producer, and consumer remain the
preferred evidence.

Do not add a broad `**/*.dart` or generated-directory ignore merely to reduce
prompt size. Apex Ray already removes generated snippets while retaining the
semantic edges needed by Freezed, JSON serialization, dependency injection,
routers, mocks, and generated clients.

## Performance And Cache

Apex Ray starts one language-server process for the review, opens changed
handwritten Dart files plus a bounded set of reverse-dependent local-package
anchors, and constrains retained source, indexes, notifications, and semantic
requests with explicit caps and the global analyzer deadline. Generated and
external SDK locations are filtered before context snippets are built.
The language server may resolve the whole workspace locally, but Apex Ray only
materializes reference, call, contract, native-channel, and related-test
snippets from the discovered project inventory after Git and `review.ignore`
filters. Ignored source is therefore not serialized into reports or sent to an
LLM.

Successful complete results use the existing analyzer index cache. The cache
fingerprint includes the selected command, Dart analyzer configuration, diff,
Dart sources, `pubspec` and lock files, analysis options, FVM selection, and
package configuration. This semantic fingerprint intentionally includes
generated Dart and analysis inputs even when they are excluded from review
context, follows bounded repository-local analysis-options includes (including
`package:` includes in a monorepo), and invalidates when those inputs change.
Reusable analyzer caching is disabled for Pub path dependencies outside the
repository because their source can change independently of the repository
fingerprint. Refresh it while diagnosing a stale environment:

```bash
apex-ray review --worktree --no-llm --refresh-analyzer-cache
```

Use `review.analyzer.index_cache_dir` or `APEX_RAY_CACHE_HOME` to place the
cache on a persistent CI volume. Do not commit the cache.

## Fallback And Troubleshooting

The Dart backend is independent from the TypeScript, Python, and Go backends.
A missing SDK, unresolved command, server startup/protocol failure, timeout,
or per-file error produces an analyzer warning and diff-only context for the
affected Dart files; it does not discard successful results from other
languages.

Start with:

```bash
apex-ray doctor
apex-ray review --worktree --no-llm --json .apex-ray/reports/dart-smoke.json
```

Common fixes:

- `Dart analyzer available: false`: select the project's Flutter/Dart SDK,
  activate FVM, or configure `review.analyzer.dart.command`.
- package imports are unresolved: run `flutter pub get` or `dart pub get` in
  the relevant package/workspace using the same SDK shown by `doctor`.
- a large review becomes partial: reduce the diff or lower semantic fan-out;
  increase `review.analyzer.timeout_seconds` only after checking dependency
  resolution and analyzer warnings.
- Flutter metadata is absent in a custom layout: verify that Flutter is
  detected, then use `flutter: enabled` when auto-detection cannot see the
  manifest/import boundary.
- results appear stale after changing SDK or package metadata: use
  `--refresh-analyzer-cache` once.

See the Dart documentation for the [analysis server and LSP
interface](https://github.com/dart-lang/sdk/tree/main/pkg/analysis_server),
the [analyzer plugin execution model](https://dart.dev/tools/analyzer-plugins),
the [Flutter SDK archive](https://docs.flutter.dev/install/archive), and
[`pub get` package resolution](https://dart.dev/tools/pub/cmd/pub-get).
