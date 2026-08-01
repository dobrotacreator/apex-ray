import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import ts from "typescript";

import { REPO_INDEX_SEMANTIC_ENTRY_LIMIT } from "../dist/constants.js";
import { collectDiInjectionIndex, collectDiProviderIndex } from "../dist/indexes/di.js";

test("DI index captures provider arrays, module providers, exports, and injections", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-di-index");
  const absPath = path.join(repo, "src/module.ts");
  const text = [
    "const CART_TOKEN = Symbol('cart');",
    "class CartService {}",
    "class PlainService {}",
    "class ExistingService {}",
    "class Repo {}",
    "const PROVIDERS = [",
    "  PlainService,",
    "  { provide: CART_TOKEN, useClass: CartService, inject: [Repo] },",
    "  { provide: ExistingService, useExisting: CartService },",
    "];",
    "@Module({ providers: [PROVIDERS, ...PROVIDERS], exports: [CartService] })",
    "class CartModule {}",
    "function handler(@Inject(CART_TOKEN) service: CartService): void {}",
  ].join("\n");
  const source = ts.createSourceFile(absPath, text, ts.ScriptTarget.ES2022, true, ts.ScriptKind.TS);

  const providers = collectDiProviderIndex(repo, source);
  const injections = collectDiInjectionIndex(repo, source);

  assert.ok(
    providers.some(
      (item) => item.tokenName === "CART_TOKEN" && item.implementationName === "CartService" && item.sourceArrayName === "PROVIDERS",
    ),
  );
  assert.ok(
    providers.some(
      (item) => item.tokenName === "PlainService" && item.implementationName === "PlainService" && item.sourceArrayName === "PROVIDERS",
    ),
  );
  assert.ok(providers.some((item) => item.tokenName === "CART_TOKEN" && item.implementationName === "Repo"));
  assert.ok(
    providers.some(
      (item) => item.tokenName === "ExistingService" && item.implementationName === "CartService" && item.sourceArrayName === "PROVIDERS",
    ),
  );
  assert.ok(providers.some((item) => item.tokenName === "PROVIDERS" && item.implementationName === "PROVIDERS"));
  assert.ok(providers.some((item) => item.tokenName === "CartService" && item.implementationName === "CartService"));
  assert.ok(injections.some((item) => item.tokenName === "CART_TOKEN"));
});

test("DI inject spreads expand only direct const arrays", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-di-inject-spread");
  const absPath = path.join(repo, "src/module.ts");
  const text = [
    "const CART_TOKEN = Symbol('cart');",
    "class DirectDep {}",
    "class ExtraOne {}",
    "class ExtraTwo {}",
    "class InlineDep {}",
    "class NestedDep {}",
    "class MutableDep {}",
    "class ShadowedTopLevelDep {}",
    "const NESTED_DEPS = [NestedDep];",
    "const EXTRA_DEPS = [ExtraOne, ExtraTwo, ...NESTED_DEPS] as const;",
    "let MUTABLE_DEPS = [MutableDep];",
    "const SHADOWED_DEPS = [ShadowedTopLevelDep];",
    "declare function dynamicDeps(): unknown[];",
    "function nestedProvider(SHADOWED_DEPS: unknown[]) {",
    "  return { provide: CART_TOKEN, inject: [...SHADOWED_DEPS] };",
    "}",
    "const provider = {",
    "  provide: CART_TOKEN,",
    "  useClass: DirectDep,",
    "  inject: [",
    "    DirectDep,",
    "    ...EXTRA_DEPS,",
    "    ...[InlineDep],",
    "    ...MUTABLE_DEPS,",
    "    ...dynamicDeps(),",
    "  ],",
    "};",
  ].join("\n");
  const source = ts.createSourceFile(
    absPath,
    text,
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );

  const providers = collectDiProviderIndex(repo, source);
  const implementations = providers
    .filter((entry) => entry.tokenName === "CART_TOKEN")
    .map((entry) => entry.implementationName);

  assert.deepEqual(implementations, [
    "DirectDep",
    "DirectDep",
    "ExtraOne",
    "ExtraTwo",
    "InlineDep",
  ]);
  for (const invalid of [
    "EXTRA_DEPS",
    "NESTED_DEPS",
    "MUTABLE_DEPS",
    "MutableDep",
    "ShadowedTopLevelDep",
  ]) {
    assert.equal(implementations.includes(invalid), false);
  }
});

test("DI inject spread expansion preserves collection budget stops", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-di-inject-budget");
  const absPath = path.join(repo, "src/module.ts");
  const source = ts.createSourceFile(
    absPath,
    [
      "const TOKEN = Symbol('token');",
      "class First {}",
      "class Second {}",
      "class Third {}",
      "const provider = {",
      "  provide: TOKEN,",
      "  inject: [...[First, Second, Third]],",
      "};",
    ].join("\n"),
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );
  let reserved = 0;
  let stopped = false;
  const reserveEntries = (count: number): boolean => {
    if (count > 1 - reserved) {
      stopped = true;
      return false;
    }
    reserved += count;
    return true;
  };

  const providers = collectDiProviderIndex(repo, source, {
    shouldStop: () => stopped,
    reserveEntry: () => reserveEntries(1),
    reserveEntries,
    markPartial: () => undefined,
  });

  assert.deepEqual(
    providers.map((entry) => entry.implementationName),
    ["First"],
  );
  assert.equal(stopped, true);
});

test("DI provider array prepass does not consume the final output budget", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-di-provider-prepass-budget");
  const absPath = path.join(repo, "src/module.ts");
  const unusedProviders = Array.from(
    { length: 64 },
    (_, index) => `UnusedProvider${index}`,
  );
  const source = ts.createSourceFile(
    absPath,
    [
      `const UNUSED_PROVIDERS = [${unusedProviders.join(", ")}];`,
      "class RealProvider {}",
      "@Module({ providers: [RealProvider] })",
      "class RealModule {}",
    ].join("\n"),
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );
  let reserved = 0;
  let stopped = false;
  const reserveEntries = (count: number): boolean => {
    if (count > 1 - reserved) {
      stopped = true;
      return false;
    }
    reserved += count;
    return true;
  };

  const providers = collectDiProviderIndex(repo, source, {
    shouldStop: () => stopped,
    reserveEntry: () => reserveEntries(1),
    reserveEntries,
    markPartial: () => undefined,
  });

  assert.deepEqual(
    providers.map((entry) => entry.implementationName),
    ["RealProvider"],
  );
  assert.equal(reserved, 1);
});

test("DI auxiliary limits mark partial without consuming the final output budget", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-di-auxiliary-limit");
  const absPath = path.join(repo, "src/module.ts");
  const source = ts.createSourceFile(
    absPath,
    [
      `const UNUSED_PROVIDERS = [${Array.from(
        { length: REPO_INDEX_SEMANTIC_ENTRY_LIMIT + 1 },
        () => "UnusedProvider",
      ).join(",")}];`,
      "class RealProvider {}",
      "@Module({ providers: [RealProvider] })",
      "class RealModule {}",
    ].join("\n"),
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );
  let reserved = 0;
  let stopped = false;
  let partialMarks = 0;
  const reserveEntries = (count: number): boolean => {
    if (count > 1 - reserved) {
      stopped = true;
      return false;
    }
    reserved += count;
    return true;
  };
  const control = {
    shouldStop: () => stopped,
    reserveEntry: () => reserveEntries(1),
    reserveEntries,
    markPartial: () => {
      partialMarks += 1;
    },
  };

  const providers = collectDiProviderIndex(repo, source, control);

  assert.deepEqual(
    providers.map((entry) => entry.implementationName),
    ["RealProvider"],
  );
  assert.equal(reserved, 1);
  assert.equal(partialMarks, 1);
});

test("DI spread expansion prioritizes concrete providers within the output budget", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-di-spread-budget");
  const absPath = path.join(repo, "src/module.ts");
  const source = ts.createSourceFile(
    absPath,
    [
      "class RealProvider {}",
      "const PROVIDERS = [RealProvider];",
      "@Module({ providers: [...PROVIDERS] })",
      "class RealModule {}",
    ].join("\n"),
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );
  let reserved = 0;
  let stopped = false;
  const reserveEntries = (count: number): boolean => {
    if (count > 1 - reserved) {
      stopped = true;
      return false;
    }
    reserved += count;
    return true;
  };
  const control = {
    shouldStop: () => stopped,
    reserveEntry: () => reserveEntries(1),
    reserveEntries,
    markPartial: () => undefined,
  };

  const providers = collectDiProviderIndex(repo, source, control);

  assert.deepEqual(
    providers.map((entry) => entry.implementationName),
    ["RealProvider"],
  );
  assert.equal(reserved, 1);
});

test("DI traversal stops walking sibling nodes after cancellation", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-di-traversal-cancellation");
  const absPath = path.join(repo, "src/module.ts");
  const source = ts.createSourceFile(
    absPath,
    Array.from(
      { length: 2_048 },
      (_, index) => `unusedExpression${index};`,
    ).join("\n"),
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );
  let cancellationChecks = 0;

  const injections = collectDiInjectionIndex(repo, source, {
    shouldStop: () => {
      cancellationChecks += 1;
      return cancellationChecks >= 3;
    },
    reserveEntry: () => true,
    reserveEntries: () => true,
    markPartial: () => undefined,
  });

  assert.deepEqual(injections, []);
  assert.ok(
    cancellationChecks <= 4,
    `expected cancellation to stop traversal, got ${cancellationChecks} checks`,
  );
});

test("DI injection traversal stops immediately when output reservation fails", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-di-injection-reserve");
  const absPath = path.join(repo, "src/module.ts");
  const source = ts.createSourceFile(
    absPath,
    [
      "Inject(FirstToken);",
      "Inject(SecondToken);",
    ].join("\n"),
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );
  let reserveAttempts = 0;
  const control = {
    shouldStop: () => false,
    reserveEntry: () => {
      reserveAttempts += 1;
      return false;
    },
    reserveEntries: () => false,
    markPartial: () => undefined,
  };

  const injections = collectDiInjectionIndex(repo, source, control);

  assert.deepEqual(injections, []);
  assert.equal(reserveAttempts, 1);
});

test("DI inject spread skips const arrays referenced before expansion", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-ts-di-inject-mutation");
  const absPath = path.join(repo, "src/module.ts");
  const source = ts.createSourceFile(
    absPath,
    [
      "const TOKEN = Symbol('token');",
      "class IndexedOriginal {}",
      "class IndexedReplacement {}",
      "class PushedOriginal {}",
      "class PushedReplacement {}",
      "class AliasedOriginal {}",
      "class AliasedReplacement {}",
      "class CleanDep {}",
      "const INDEXED_DEPS = [IndexedOriginal];",
      "INDEXED_DEPS[0] = IndexedReplacement;",
      "const PUSHED_DEPS = [PushedOriginal];",
      "PUSHED_DEPS.push(PushedReplacement);",
      "const ALIASED_DEPS = [AliasedOriginal];",
      "const alias = ALIASED_DEPS;",
      "alias.push(AliasedReplacement);",
      "const CLEAN_DEPS = [CleanDep];",
      "const firstProvider = {",
      "  provide: TOKEN,",
      "  inject: [",
      "    ...INDEXED_DEPS,",
      "    ...PUSHED_DEPS,",
      "    ...ALIASED_DEPS,",
      "    ...CLEAN_DEPS,",
      "  ],",
      "};",
      "const secondProvider = {",
      "  provide: TOKEN,",
      "  inject: [...CLEAN_DEPS],",
      "};",
    ].join("\n"),
    ts.ScriptTarget.ES2022,
    true,
    ts.ScriptKind.TS,
  );

  const providers = collectDiProviderIndex(repo, source);
  const implementations = providers
    .filter((entry) => entry.tokenName === "TOKEN")
    .map((entry) => entry.implementationName);

  assert.deepEqual(implementations, ["CleanDep", "CleanDep"]);
});
