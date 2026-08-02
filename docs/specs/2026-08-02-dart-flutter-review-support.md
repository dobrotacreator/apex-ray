# Dart and Flutter Review Support Spec

## Goal

Add production-ready Dart and Flutter review to Apex Ray without weakening the existing TypeScript, Python, and Go paths. A Dart-only or mixed-language diff must receive repository-aware semantic context, Flutter-specific review guidance, bounded runtime and memory use, deterministic fallback behavior, and installed-package/CI coverage.

The quality target is not extension recognition alone. Changed Dart declarations should be connected to callers, callees, type and framework contracts, related tests, generated interfaces, navigation and state-management boundaries, and native platform-channel consumers when those relationships are available.

## Privacy And Validation Inputs

A private, large Flutter application may be used only as a local validation target. No private source, identifiers, paths, package names, dependency lists, reports, or raw telemetry may be committed. Durable regressions discovered there must be recreated with neutral, synthetic fixtures. Only coarse anonymous performance characteristics may be recorded.

## Architectural Decision

Use the Dart Analysis Server through its standard LSP transport, launched from the Dart SDK already selected for the reviewed project.

Do not bundle a Dart or Flutter SDK and do not embed `package:analyzer` in Apex Ray. The language server is the supported tool-integration boundary, matches the project's Dart/Flutter version, resolves the project's package graph and generated declarations, and exposes document symbols, references, definitions, call and type hierarchy, diagnostics, Dart outline, and Flutter outline.

`dart analyze` remains a user/CI diagnostics command and is not the semantic backend: it cannot supply the context graph needed by Apex Ray.

## SDK Resolution

Resolve the command without executing package installation or changing the reviewed project:

1. explicit `review.analyzer.dart.command` configuration;
2. a project-local `.fvm/flutter_sdk/bin/dart` executable;
3. `dart` on `PATH`;
4. `fvm dart` when FVM is on `PATH`;
5. the Dart binary adjacent to a discoverable Flutter SDK when its location is unambiguous.

The configured command is an argument list, not a shell string. Apex Ray appends `language-server`, `--client-id`, and `--client-version`. It never evaluates a shell, downloads an SDK, runs `pub get`, or invokes build generation.

`apex-ray doctor` reports the resolved command, SDK version when it can be queried within a short timeout, project-local/FVM selection, and a clear remediation when no SDK is available.

## Configuration

Add a backwards-compatible nested Dart configuration under `review.analyzer`:

```yaml
review:
  analyzer:
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

Defaults must be useful for ordinary projects while bounding large diffs. `flutter` accepts `auto`, `enabled`, or `disabled`. The existing global analyzer timeout, cache, and refresh controls apply; the single-process Dart backend additionally owns explicit changed-source, anchor, index, notification, and semantic fan-out limits. An unavailable or disabled Dart backend never prevents other analyzers from succeeding.

## Discovery And Classification

- recognize `.dart` as `dart`;
- recognize `pubspec.yaml` and `pubspec.yml` as dependency configuration;
- report `pub` as a package manager and Flutter as a framework when the manifest declares an SDK Flutter dependency;
- preserve `pubspec.lock` as a lockfile;
- recognize `test/` and `integration_test/` plus `*_test.dart` as tests;
- classify common generated Dart suffixes such as `.g.dart`, `.freezed.dart`, `.config.dart`, `.mocks.dart`, `.gr.dart`, and `.chopper.dart` as generated;
- keep generated files in project inventory for semantic resolution, but exclude them from changed review targets, findings, related-test snippets, and ordinary prompt context;
- add Dart/Flutter boundary risk signals for routing, permissions, platform channels, persistence, serialization, authentication, and state/lifecycle changes using conservative path and syntax evidence.

## LSP Process Lifecycle

Use one long-lived server per review run, not one process per file or shard.

- communicate with JSON-RPC/LSP `Content-Length` framing over stdin/stdout;
- drain stderr concurrently into a bounded diagnostic buffer;
- use process-group termination and guarantee cleanup on success, error, cancellation, and timeout;
- respond to supported server-to-client requests such as configuration and capability registration;
- initialize with `onlyAnalyzeProjectsWithOpenFiles: true`, Dart outline, and Flutter outline in auto/enabled mode;
- open changed handwritten Dart files and a bounded set of reverse-dependent local-package anchors required for cross-package semantic requests;
- wait for workspace analysis completion when the server advertises the capability, otherwise let semantic requests provide the synchronization boundary;
- use independent request deadlines constrained by the global analyzer deadline;
- tolerate unknown notifications and forward-compatible response fields;
- treat malformed framing, process exit, unsupported methods, and timeouts as explicit backend warnings with per-file diff-only fallback.

The client must support UTF-16 LSP positions and convert them to Apex Ray's one-based line ranges. File URIs must round-trip correctly on macOS, Linux, and Windows, including spaces and non-ASCII paths. Locations outside the repository are ignored.

## Semantic Mapping

Flatten `DocumentSymbol` or `SymbolInformation` results into the shared `AnalyzerResult` contract.

Collect declarations for classes, mixins, extensions, extension types, enums, enum members, constructors, methods, getters/setters, fields, top-level variables, functions, and typedefs. Preserve nested symbols, signatures/details, exact declaration ranges, and Dart privacy (`_name`) as the exported flag.

A symbol is changed when its declaration/code range intersects added lines. Deleted-only changes use adjacent declarations where reliable and otherwise produce a file-level fallback pack.

For changed symbols, query within configured caps:

- `textDocument/references` for repository consumers;
- call hierarchy incoming/outgoing calls for callers and callees;
- type hierarchy supertypes/subtypes for contracts;
- definitions/hover only when needed to resolve a high-value boundary;
- diagnostics and outline notifications as metadata, never as automatic findings.

Parse Dart import/export/part directives with a bounded lexical scanner. Preserve `part`/`part of` relationships and package/relative imports. Do not assume the local package graph is acyclic.

Deduplicate locations, prefer handwritten repository sources over generated or SDK paths, limit snippets per context layer, and sort deterministically.

## Generated Code Policy

Generated Dart is index-only by default:

- the Dart server may read and resolve it;
- Apex Ray may use a generated declaration to connect a handwritten producer and consumer;
- raw generated snippets are not placed into LLM prompts;
- generated files never receive findings;
- references that exist only inside generated code are summarized as bounded metadata rather than copied;
- handwritten annotations, declarations, and `part` directives are preferred as the contract evidence.

This policy prevents token and memory blowups while retaining Freezed, JSON serialization, dependency-injection, router, mock, and API-client resolution.

## Related Tests

Combine semantic references with conservative conventions:

- references from `test/` and `integration_test/`;
- package-relative `lib/foo.dart` to `test/foo_test.dart` mapping;
- sibling and feature-level `*_test.dart` candidates;
- widget, golden, BLoC/state, and integration-test registration detected from imports and calls;
- cross-package tests in local path packages.

Candidates must exist, remain inside the repository, exclude generated files, be ranked by semantic evidence before naming heuristics, and respect configured limits.

## Flutter-Aware Context

When Flutter is detected or explicitly enabled, enrich changed symbols without inventing findings:

- Flutter outline for widget composition tied to the changed declaration;
- `StatefulWidget` to `State<T>` and `createState` relationships;
- lifecycle methods, `mounted` checks, `setState`, controllers, focus nodes, subscriptions, listeners, timers, streams, and their cleanup sites;
- `BuildContext` use across async gaps;
- BLoC/Cubit event-state-producer/consumer relationships;
- MobX observable/action/reaction/Observer relationships;
- provider-style and service-locator dependency boundaries, including common DI annotations;
- GoRouter route, redirect, parameter, nested route, shell route, and navigation-call relationships;
- direct Navigator calls and returned-result contracts;
- serialization/codegen annotations and handwritten model contracts;
- permissions, secure storage, local persistence, networking, isolates, and background work;
- MethodChannel/EventChannel/BasicMessageChannel names and methods.

Framework adapters are bounded metadata collectors over generic concepts. They should activate only from concrete imports, resolved symbols, annotations, or call sites and must degrade safely for custom abstractions.

## Platform Channels

Build a small literal index for changed Dart platform-channel declarations and repository Kotlin/Java/Swift/Objective-C handlers. Match exact channel and method literals, retain only repository locations, and surface handler signatures as metadata/contracts. Do not run the non-Dart analyzers solely to populate this index. Dynamic channel names remain unsupported and should not produce speculative links.

## Prompt And Review Behavior

Add Dart and Flutter language guidance to deep, shallow, and verifier prompts. Priorities include:

- lifecycle ownership and cleanup;
- async `BuildContext` and stale widget state;
- state transition and stream error semantics;
- navigation/redirect loops and parameter contracts;
- serialization compatibility and generated-interface drift;
- platform-channel name/type/error parity;
- permission, storage, network, and background-execution boundaries;
- widget accessibility, responsiveness, rebuild scope, and test coverage when evidence is present.

Prompts must explicitly treat diagnostics as context, not review findings, and avoid reporting style/lint issues already handled by `dart analyze` or `flutter_lints`. Bump prompt versions.

## Performance And Cache

- keep a single server process and open only changed files plus bounded local-package anchors;
- cap semantic requests per file/symbol and short-circuit layers after the global deadline;
- exclude generated snippets before context construction;
- reuse the existing analyzer index-cache surface for a compact Dart result cache keyed by Apex Ray analyzer version, SDK version, relevant configuration, project/package metadata, file identity, and diff ranges;
- invalidate when `pubspec.yaml`, `pubspec.lock`, `analysis_options.yaml`, `.dart_tool/package_config.json`, or linked package manifests change;
- never cache absolute paths in portable committed artifacts;
- record backend duration, partial/fallback reasons, file/symbol/request counts, generated references suppressed, and cache hit/miss data without source contents.

The private large-project benchmark must verify bounded memory, prompt characters/tokens, and repeat-run latency. No raw report or private telemetry is committed.

## Test Strategy

Protocol behavior must not depend on a locally installed SDK. Add a deterministic fake LSP server that covers framing, out-of-order notifications, reverse requests, errors, timeout, process exit, UTF-16 positions, multiple packages, generated locations, diagnostics, symbols, references, call/type hierarchy, and Flutter outline.

Add synthetic fixtures for:

- Dart declarations/imports/parts/extensions/mixins/sealed types/async code;
- multi-package graphs including a cycle;
- generated Freezed/JSON/DI/router files with prompt exclusion assertions;
- widgets, lifecycle/resource cleanup, and async context;
- BLoC/Cubit, MobX, and dependency injection;
- GoRouter/Navigator;
- unit, widget, golden, and integration tests;
- Dart-to-Kotlin/Swift platform channels;
- a generated-heavy scale fixture with deterministic budget assertions.

CI additionally installs a pinned Flutter stable version and runs a live installed-wheel no-LLM smoke review. The smoke must prove Dart analyzer results, changed symbols, at least one semantic reference/test relationship, and no generated prompt snippets. Fake-server tests remain the primary exhaustive protocol coverage.

## Documentation And Packaging

- update README, architecture, configuration, development, CI/GitHub Actions, and troubleshooting documentation;
- document that consumers provide a Dart/Flutter SDK and resolved dependencies;
- document automatic/FVM/configured command selection and safe fallbacks;
- include all new Python modules and synthetic fixtures required by the wheel tests;
- add `doctor` output and installed-wheel coverage;
- describe generated-code handling and supported Flutter relationships without claiming perfect dynamic resolution.

## Acceptance Criteria

- Dart and Flutter are discovered and classified correctly with existing configs remaining valid.
- A Dart diff produces non-fallback semantic context when a compatible SDK is present.
- Missing SDK, unresolved packages, server failure, partial responses, or timeout produce actionable warnings and diff-only packs without breaking other languages.
- Changed symbols include exact ranges and bounded repository references, callers, callees, contracts, metadata, and related tests.
- Local packages, cycles, `part` files, UTF-16 text, spaces, and non-ASCII paths are handled.
- Generated files participate in resolution but never become review targets or raw prompt snippets.
- Flutter widget/state/lifecycle, state-management, routing, serialization, and exact platform-channel relationships are represented when concrete evidence exists.
- Cache and request limits materially reduce repeated-run work and large-project prompt volume.
- Existing TypeScript, Python, and Go tests remain green.
- Formatting, lint, typecheck, full pytest, TypeScript analyzer tests, build, metadata check, installed-wheel smoke, and live Dart/Flutter CI smoke pass.
- A local anonymized run against the private Flutter validation project completes within configured bounds and produces useful context without committing private artifacts.
- Apex Ray's own diff review completes before the PR is opened.

## Delivery

Deliver as one PR with internally reviewable commits if useful. The PR targets `main`, uses a Conventional Commit title, and explicitly documents SDK prerequisites, fallback behavior, performance limits, validation performed, and remaining known dynamic-language limitations.
