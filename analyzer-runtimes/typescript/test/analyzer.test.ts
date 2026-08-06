import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import ts from "typescript";

import { analyze } from "../dist/analyzer.js";
import { parseArgs } from "../dist/cli.js";
import { repoIndexCachePath } from "../dist/indexes/repo-cache.js";
import {
  createProgramContexts,
  selectSupplementalDeclarationRoots,
} from "../dist/program.js";
import type { AnalyzerResult } from "../dist/types.js";
import { loadRepoFileInventory } from "../dist/workspace/inventory.js";
import { writeFile } from "./helpers.js";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const analyzerRoot = path.resolve(testDir, "..");
const analyzerScript = path.join(analyzerRoot, "dist", "analyze.js");

function runAnalyzer(repo: string, args: string[] = []): AnalyzerResult {
  const stdout = execFileSync(
    process.execPath,
    [analyzerScript, "--repo", repo, "--changed", ...args],
    { encoding: "utf8" },
  );
  return JSON.parse(stdout) as AnalyzerResult;
}

function runAnalyzerInProcess(repo: string, args: string[] = []): AnalyzerResult {
  return analyze(parseArgs(["--repo", repo, "--changed", ...args]));
}

test("analyzer reports changed symbols, call references, and contracts", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-"));
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
        },
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(
      repo,
      "src/cart.ts",
      [
        "export interface CartItem {",
        "  price: number;",
        "  quantity: number;",
        "}",
        "",
        "export function calculateTotal(items: CartItem[]): number {",
        "  return items.reduce((total, item) => total + item.price * item.quantity, 0);",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "src/checkout.ts",
      [
        "import { calculateTotal, type CartItem } from './cart.js';",
        "",
        "export function checkout(items: CartItem[]): number {",
        "  return calculateTotal(items);",
        "}",
      ].join("\n"),
    );

    const result = runAnalyzer(repo, ["src/cart.ts", "--range", "src/cart.ts:6-8", "--no-index-cache"]);

    assert.equal(result.language, "typescript");
    assert.equal(result.files.length, 1);
    assert.equal(result.files[0].path, "src/cart.ts");
    assert.deepEqual(result.warnings, []);
    assert.deepEqual(result.coverage, {
      partial: false,
      reasonCodes: [],
      scopes: [],
      failedFileCount: 0,
    });
    assert.ok(result.metrics.wallDurationMs >= 0);
    assert.deepEqual(Object.keys(result.metrics.stageDurationsMs), [
      "inventory",
      "program_contexts",
      "workspace_index",
      "changed_files",
    ]);
    assert.equal(result.metrics.shards.length, 1);
    assert.equal(result.metrics.shards[0].changedFileCount, 1);
    assert.equal(result.metrics.shards[0].analyzedFileCount, 1);
    assert.equal(result.metrics.shards[0].status, "complete");

    const changedSymbol = result.files[0].changedSymbols.find((symbol) => symbol.name === "calculateTotal");
    assert.ok(changedSymbol);
    assert.equal(changedSymbol.kind, "function");
    assert.match(changedSymbol.signature, /CartItem\[\]\): number/);
    assert.ok(
      changedSymbol.references.some(
        (reference) =>
          reference.kind === "call" &&
          reference.file === "src/checkout.ts" &&
          reference.text.includes("return calculateTotal(items);"),
      ),
    );
    assert.ok(
      changedSymbol.contracts.some(
        (reference) =>
          reference.kind === "contract" &&
          reference.file === "src/cart.ts" &&
          reference.text.includes("export interface CartItem"),
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("analyzer library API matches the CLI JSON contract", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-api-"));
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
        },
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(
      repo,
      "src/service.ts",
      [
        "export class Service {",
        "  changed(id) {",
        "    return id;",
        "  }",
        "",
        "  caller(id) {",
        "    return this.changed(id);",
        "  }",
        "}",
      ].join("\n"),
    );

    const args = ["src/service.ts", "--range", "src/service.ts:2-4", "--no-index-cache"];
    const cliResult = runAnalyzer(repo, args);
    const apiResult = runAnalyzerInProcess(repo, args);

    const { metrics: cliMetrics, ...cliStableResult } = cliResult;
    const { metrics: apiMetrics, ...apiStableResult } = apiResult;
    assert.deepEqual(apiStableResult, cliStableResult);
    assertAnalyzerMetricDurations(cliMetrics);
    assertAnalyzerMetricDurations(apiMetrics);
    assert.deepEqual(analyzerMetricsShape(apiMetrics), analyzerMetricsShape(cliMetrics));
    const changedSymbol = apiResult.files[0].changedSymbols.find((symbol) => symbol.name === "changed");
    assert.ok(changedSymbol);
    assert.ok(
      changedSymbol.references.some(
        (reference) => reference.kind === "call" && reference.text.includes("return this.changed(id);"),
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

function assertAnalyzerMetricDurations(metrics: AnalyzerResult["metrics"]): void {
  assertFiniteNonNegativeDuration(metrics.wallDurationMs, "analyzer wall duration");
  for (const [stage, duration] of Object.entries(metrics.stageDurationsMs)) {
    assertFiniteNonNegativeDuration(duration, `analyzer stage ${stage}`);
  }
  for (const shard of metrics.shards) {
    assertFiniteNonNegativeDuration(shard.wallDurationMs, `shard ${shard.index} wall duration`);
    for (const [stage, duration] of Object.entries(shard.stageDurationsMs)) {
      assertFiniteNonNegativeDuration(duration, `shard ${shard.index} stage ${stage}`);
    }
  }
}

function assertFiniteNonNegativeDuration(value: number, label: string): void {
  assert.ok(Number.isFinite(value) && value >= 0, `${label} must be finite and non-negative`);
}

function analyzerMetricsShape(metrics: AnalyzerResult["metrics"]): object {
  return {
    stageNames: Object.keys(metrics.stageDurationsMs),
    shards: metrics.shards.map(
      ({ wallDurationMs, stageDurationsMs, ...shard }) => ({
        ...shard,
        stageNames: Object.keys(stageDurationsMs),
        hasNonNegativeWallDuration: wallDurationMs >= 0,
      }),
    ),
    hasNonNegativeWallDuration: metrics.wallDurationMs >= 0,
  };
}

test("configured programs analyze changed JavaScript roots when allowJs is disabled", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-configured-js-"));
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
        },
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(repo, "src/index.ts", "export const typed = true;\n");
    writeFile(repo, "src/module.mjs", "export function esmValue() { return 1; }\n");
    writeFile(
      repo,
      "src/module.cjs",
      "function cjsValue() { return 2; }\nmodule.exports = { cjsValue };\n",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/module.mjs",
      "src/module.cjs",
      "--range",
      "src/module.mjs:1-1",
      "--range",
      "src/module.cjs:1-1",
      "--no-index-cache",
    ]);

    const changedSymbolsByFile = new Map(
      result.files.map((file) => [
        file.path,
        new Set(file.changedSymbols.map((symbol) => symbol.name)),
      ]),
    );
    assert.equal(changedSymbolsByFile.get("src/module.mjs")?.has("esmValue"), true);
    assert.equal(changedSymbolsByFile.get("src/module.cjs")?.has("cjsValue"), true);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("stable compiler and config reads preserve TypeScript BOM decoding", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-utf16-"),
  );
  try {
    const configText = JSON.stringify({
      compilerOptions: {
        module: "NodeNext",
        moduleResolution: "NodeNext",
        target: "ES2022",
      },
      files: ["src/changed.ts"],
    });
    const sourceText =
      'export function visible(): "UTF16_VISIBLE" { return "UTF16_VISIBLE"; }\n';
    writeFile(repo, "tsconfig.json", "");
    writeFile(repo, "src/changed.ts", "");
    fs.writeFileSync(
      path.join(repo, "tsconfig.json"),
      Buffer.concat([
        Buffer.from([0xff, 0xfe]),
        Buffer.from(configText, "utf16le"),
      ]),
    );
    fs.writeFileSync(
      path.join(repo, "src", "changed.ts"),
      Buffer.concat([
        Buffer.from([0xff, 0xfe]),
        Buffer.from(sourceText, "utf16le"),
      ]),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["src/changed.ts"],
        config_files: ["tsconfig.json"],
      }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/changed.ts",
      "--range",
      "src/changed.ts:1-1",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const visible = result.files[0].symbols.find(
      (symbol) => symbol.name === "visible",
    );
    assert.equal(visible?.signature, '(): "UTF16_VISIBLE"');
    assert.equal(result.partial, false);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("analyzer loads permitted ambient declarations from the manifest without a tsconfig", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-ambient-manifest-"));
  try {
    writeFile(
      repo,
      "src/globals.d.mts",
      [
        "export {};",
        "declare global {",
        "  interface PaymentReceipt {",
        "    id: string;",
        "    amount: number;",
        "  }",
        "  function charge(): PaymentReceipt;",
        "}",
      ].join("\n"),
    );
    writeFile(repo, "src/consumer.ts", "export function settle() { return charge(); }\n");
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/consumer.ts", "src/globals.d.mts"] }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/consumer.ts",
      "--range",
      "src/consumer.ts:1-1",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    assert.equal(result.files[0].symbols[0].signature, "(): PaymentReceipt");
    assert.ok(result.warnings.some((warning) => warning.includes("1 permitted declaration root")));
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("analyzer reports missing manifest declaration roots as partial", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-ambient-missing-"),
  );
  try {
    writeFile(
      repo,
      "src/consumer.ts",
      "export function settle() { return missingAmbientValue(); }\n",
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["src/consumer.ts", "types/missing-global.d.ts"],
      }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/consumer.ts",
      "--range",
      "src/consumer.ts:1-1",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    assert.equal(result.files[0].symbols[0].signature, "(): any");
    assert.equal(result.partial, true);
    assert.ok(
      result.warnings.some(
        (warning) =>
          warning.includes("types/missing-global.d.ts") &&
          warning.includes("unavailable"),
      ),
    );
    assert.equal(
      result.warnings.some((warning) =>
        warning.includes("1 permitted declaration root")
      ),
      false,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("no-tsconfig programs isolate ambient declarations by package boundary", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-ambient-package-scope-"));
  try {
    writeFile(repo, "packages/a/package.json", JSON.stringify({ name: "@example/a" }));
    writeFile(
      repo,
      "packages/a/globals.d.ts",
      [
        "export {};",
        "declare global {",
        "  interface PaymentReceipt { leaked: string; }",
        "}",
      ].join("\n"),
    );
    writeFile(repo, "packages/b/package.json", JSON.stringify({ name: "@example/b" }));
    writeFile(
      repo,
      "packages/b/globals.d.ts",
      [
        "export {};",
        "declare global {",
        "  interface PaymentReceipt { id: string; }",
        "  function charge(): PaymentReceipt;",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "packages/b/consumer.ts",
      [
        "export function receiptId() { return charge().id; }",
        "export function leakedValue() { return charge().leaked; }",
      ].join("\n"),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [
          "packages/a/globals.d.ts",
          "packages/b/consumer.ts",
          "packages/b/globals.d.ts",
        ],
      }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "packages/b/consumer.ts",
      "--range",
      "packages/b/consumer.ts:1-2",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const signatures = new Map(result.files[0].symbols.map((symbol) => [symbol.name, symbol.signature]));
    assert.equal(signatures.get("receiptId"), "(): string");
    assert.equal(signatures.get("leakedValue"), "(): any");
    assert.ok(result.warnings.some((warning) => warning.includes("1 permitted declaration root")));
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("no-tsconfig programs isolate global augmentations in ordinary source files", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-global-isolation-"));
  try {
    for (const packageName of ["alpha", "beta"]) {
      writeFile(
        repo,
        `packages/${packageName}/package.json`,
        JSON.stringify({ name: `@workspace/${packageName}`, type: "module" }),
      );
    }
    writeFile(
      repo,
      "packages/alpha/src/globals.ts",
      [
        "export {};",
        "declare global {",
        "  function siblingLeak(): string;",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "packages/beta/src/consumer.ts",
      "export function observed() { return siblingLeak(); }\n",
    );

    const result = runAnalyzerInProcess(repo, [
      "packages/alpha/src/globals.ts",
      "packages/beta/src/consumer.ts",
      "--range",
      "packages/alpha/src/globals.ts:1-4",
      "--range",
      "packages/beta/src/consumer.ts:1-1",
      "--no-index-cache",
    ]);

    const consumer = result.files.find(
      (file) => file.path === "packages/beta/src/consumer.ts",
    );
    const observed = consumer?.symbols.find((symbol) => symbol.name === "observed");
    assert.equal(observed?.signature, "(): any");
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("no-tsconfig packages inherit manifest-permitted workspace ambient declarations", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-workspace-ambient-"));
  try {
    writeFile(repo, "package.json", JSON.stringify({ private: true }));
    writeFile(repo, "types/globals.d.ts", "declare function workspaceCharge(): bigint;\n");
    writeFile(repo, "packages/a/package.json", JSON.stringify({ name: "@example/a" }));
    writeFile(
      repo,
      "packages/a/consumer.ts",
      "export function settleWorkspace() { return workspaceCharge(); }\n",
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["packages/a/consumer.ts", "types/globals.d.ts"],
      }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "packages/a/consumer.ts",
      "--range",
      "packages/a/consumer.ts:1-1",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    assert.equal(result.files[0].symbols[0].signature, "(): bigint");
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("invalid shared tsconfig falls back independently for each package", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-invalid-config-scope-"));
  try {
    writeFile(repo, "tsconfig.json", "{");
    writeFile(repo, "packages/a/package.json", JSON.stringify({ name: "@example/a" }));
    writeFile(repo, "packages/a/globals.d.ts", "declare function localA(): string;\n");
    writeFile(repo, "packages/a/consumer.ts", "export function fromA() { return localA(); }\n");
    writeFile(repo, "packages/b/package.json", JSON.stringify({ name: "@example/b" }));
    writeFile(repo, "packages/b/globals.d.ts", "declare function localB(): number;\n");
    writeFile(repo, "packages/b/consumer.ts", "export function fromB() { return localB(); }\n");
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [
          "packages/a/consumer.ts",
          "packages/a/globals.d.ts",
          "packages/b/consumer.ts",
          "packages/b/globals.d.ts",
        ],
      }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "packages/a/consumer.ts",
      "packages/b/consumer.ts",
      "--range",
      "packages/a/consumer.ts:1-1",
      "--range",
      "packages/b/consumer.ts:1-1",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const signaturesByFile = new Map(
      result.files.map((file) => [
        file.path,
        new Map(file.symbols.map((symbol) => [symbol.name, symbol.signature])),
      ]),
    );
    assert.equal(signaturesByFile.get("packages/a/consumer.ts")?.get("fromA"), "(): string");
    assert.equal(signaturesByFile.get("packages/b/consumer.ts")?.get("fromB"), "(): number");
    assert.equal(result.warnings.filter((warning) => warning.includes("expected")).length, 1);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("configured programs retain only ambient declarations permitted by the manifest", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-focused-ambient-"));
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: { target: "ES2022", strict: true },
        include: ["src/**/*", "generated/**/*"],
      }),
    );
    writeFile(
      repo,
      "src/globals.d.cts",
      [
        "export {};",
        "declare global {",
        "  interface PaymentReceipt {",
        "    id: string;",
        "  }",
        "  function charge(): PaymentReceipt;",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "generated/ignored.d.mts",
      [
        "export {};",
        "declare global {",
        "  interface IgnoredReceipt {",
        "    secret: string;",
        "  }",
        "  function ignoredCharge(): IgnoredReceipt;",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "src/consumer.ts",
      [
        "export function settle() { return charge(); }",
        "export function ignoredSettle() { return ignoredCharge(); }",
      ].join("\n"),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/consumer.ts", "src/globals.d.cts"] }),
      "utf8",
    );

    const analyzerArgs = [
      "src/consumer.ts",
      "--range",
      "src/consumer.ts:1-2",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ];
    const normalResult = runAnalyzerInProcess(repo, analyzerArgs);
    const focusedResult = runAnalyzerInProcess(repo, [
      ...analyzerArgs,
      "--large-change-set-size",
      "40",
    ]);

    for (const result of [normalResult, focusedResult]) {
      const signatures = new Map(result.files[0].symbols.map((symbol) => [symbol.name, symbol.signature]));
      assert.equal(signatures.get("settle"), "(): PaymentReceipt");
      assert.equal(signatures.get("ignoredSettle"), "(): any");
    }
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("configured programs exclude tsconfig roots outside the repository", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-external-root-"),
  );
  const outside = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-external-source-"),
  );
  try {
    const outsideSource = path.join(outside, "private.d.ts");
    fs.writeFileSync(
      outsideSource,
      'declare function externalValue(): "PRIVATE_LITERAL";\n',
      "utf8",
    );
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          moduleResolution: "Bundler",
          preserveSymlinks: true,
          target: "ES2022",
        },
        files: [
          "src/changed.ts",
          path.relative(repo, outsideSource).replaceAll("\\", "/"),
        ],
      }),
    );
    writeFile(
      repo,
      "src/changed.ts",
      "export function visible() { return externalValue(); }\n",
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/changed.ts"] }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/changed.ts",
      "--range",
      "src/changed.ts:1-1",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const visible = result.files[0].symbols.find(
      (symbol) => symbol.name === "visible",
    );
    assert.equal(visible?.signature, "(): any");
    assert.equal(JSON.stringify(result).includes("PRIVATE_LITERAL"), false);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
});

test("configured programs do not read extended configs outside the repository", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-external-config-"),
  );
  const outside = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-private-config-"),
  );
  try {
    const privateConfig = path.join(outside, "private.json");
    fs.writeFileSync(
      privateConfig,
      JSON.stringify({
        compilerOptions: {
          PRIVATE_CONFIG_LITERAL: true,
        },
      }),
      "utf8",
    );
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        extends: path.relative(repo, privateConfig).replaceAll("\\", "/"),
        include: ["src/**/*"],
      }),
    );
    writeFile(
      repo,
      "src/changed.ts",
      "export function visible() { return 1; }\n",
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/changed.ts"] }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/changed.ts",
      "--range",
      "src/changed.ts:1-1",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    assert.equal(
      JSON.stringify(result).includes("PRIVATE_CONFIG_LITERAL"),
      false,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
});

test("manifest-backed programs do not read config files omitted from the inventory", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-omitted-config-"),
  );
  const cacheDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-omitted-config-cache-"),
  );
  try {
    writeFile(
      repo,
      "private.json",
      JSON.stringify({
        compilerOptions: {
          PRIVATE_IGNORED_CONFIG_LITERAL: true,
        },
      }),
    );
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        extends: "./private.json",
        include: ["src/**/*"],
      }),
    );
    writeFile(
      repo,
      "src/changed.ts",
      "export function visible() { return 1; }\n",
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/changed.ts"] }),
      "utf8",
    );

    const analyzerArgs = [
      "src/changed.ts",
      "--range",
      "src/changed.ts:1-1",
      "--file-manifest",
      manifestPath,
      "--index-cache-dir",
      cacheDir,
    ];
    const result = runAnalyzerInProcess(repo, analyzerArgs);
    const cachedResult = runAnalyzerInProcess(repo, analyzerArgs);

    assert.equal(result.files.length, 1);
    assert.equal(result.partial, true);
    assert.equal(result.indexCache?.written, true);
    assert.equal(cachedResult.indexCache?.hits, 1);
    assert.equal(cachedResult.indexCache?.misses, 0);
    assert.equal(
      JSON.stringify(result).includes("PRIVATE_IGNORED_CONFIG_LITERAL"),
      false,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("analyzer ignores repository config symlinks that resolve outside", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-config-symlink-"),
  );
  const outside = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-config-symlink-target-"),
  );
  try {
    const privateConfig = path.join(outside, "tsconfig.json");
    fs.writeFileSync(
      privateConfig,
      JSON.stringify({
        compilerOptions: {
          PRIVATE_SYMLINKED_CONFIG_LITERAL: true,
        },
      }),
      "utf8",
    );
    fs.symlinkSync(privateConfig, path.join(repo, "tsconfig.json"));
    writeFile(
      repo,
      "src/changed.ts",
      "export function visible() { return 1; }\n",
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/changed.ts"] }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/changed.ts",
      "--range",
      "src/changed.ts:1-1",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    assert.equal(result.tsconfigPath, null);
    assert.equal(
      JSON.stringify(result).includes("PRIVATE_SYMLINKED_CONFIG_LITERAL"),
      false,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
});

test("config reads reject a path swapped to an external symlink after validation", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-config-race-"),
  );
  const outside = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-config-race-target-"),
  );
  const originalRealpathSync = fs.realpathSync;
  try {
    const configPath = path.join(repo, "tsconfig.json");
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({ include: ["src/**/*"] }),
    );
    const outsideConfigPath = path.join(outside, "private.json");
    fs.writeFileSync(
      outsideConfigPath,
      JSON.stringify({
        compilerOptions: {
          PRIVATE_CONFIG_RACE_LITERAL: true,
        },
      }),
      "utf8",
    );
    writeFile(
      repo,
      "src/changed.ts",
      "export function visible() { return 1; }\n",
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/changed.ts"] }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];
    let swapped = false;

    fs.realpathSync = ((candidate: fs.PathLike, ...rest: unknown[]) => {
      const result = (originalRealpathSync as (...args: unknown[]) => unknown)(
        candidate,
        ...rest,
      );
      if (
        !swapped &&
        path.resolve(String(candidate)) === path.resolve(configPath)
      ) {
        swapped = true;
        fs.rmSync(configPath);
        fs.symlinkSync(outsideConfigPath, configPath);
      }
      return result;
    }) as typeof fs.realpathSync;

    createProgramContexts(args, warnings, inventory);

    assert.equal(swapped, true);
    assert.equal(inventory.configurationPartial, true);
    assert.equal(
      JSON.stringify(warnings).includes("PRIVATE_CONFIG_RACE_LITERAL"),
      false,
    );
  } finally {
    fs.realpathSync = originalRealpathSync;
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
});

test("manifest-backed config parsing does not scan the repository filesystem", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-config-inventory-"),
  );
  const originalReadDirectory = ts.sys.readDirectory;
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: { target: "ES2022" },
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(
      repo,
      "src/changed.ts",
      "export function visible() { return 1; }\n",
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/changed.ts"] }),
      "utf8",
    );
    ts.sys.readDirectory = () => {
      throw new Error("unexpected unbounded directory scan");
    };

    const result = runAnalyzerInProcess(repo, [
      "src/changed.ts",
      "--range",
      "src/changed.ts:1-1",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    assert.equal(result.files.length, 1);
  } finally {
    ts.sys.readDirectory = originalReadDirectory;
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("manifest metadata resolves config-only workspace package extends", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-workspace-config-"),
  );
  try {
    writeFile(
      repo,
      "packages/tsconfig/package.json",
      JSON.stringify({ name: "@workspace/tsconfig" }),
    );
    writeFile(
      repo,
      "packages/tsconfig/base.json",
      JSON.stringify({
        compilerOptions: {
          baseUrl: "../..",
          moduleResolution: "Bundler",
          paths: {
            "@shared/value": ["packages/shared/value.ts"],
          },
          target: "ES2022",
        },
      }),
    );
    writeFile(
      repo,
      "packages/shared/value.ts",
      'export function sharedValue(): "WORKSPACE_CONFIG_LITERAL" { return "WORKSPACE_CONFIG_LITERAL"; }\n',
    );
    writeFile(
      repo,
      "apps/web/tsconfig.json",
      JSON.stringify({
        extends: "@workspace/tsconfig/base.json",
        include: ["src/**/*"],
      }),
    );
    writeFile(
      repo,
      "apps/web/src/changed.ts",
      [
        "import { sharedValue } from '@shared/value';",
        "export function visible() { return sharedValue(); }",
      ].join("\n"),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [
          "apps/web/src/changed.ts",
          "packages/shared/value.ts",
        ],
        package_files: ["packages/tsconfig/package.json"],
        config_files: [
          "apps/web/tsconfig.json",
          "packages/tsconfig/base.json",
          "packages/tsconfig/package.json",
        ],
      }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "apps/web/src/changed.ts",
      "--range",
      "apps/web/src/changed.ts:1-2",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const visible = result.files[0].symbols.find(
      (symbol) => symbol.name === "visible",
    );
    assert.equal(visible?.signature, '(): "WORKSPACE_CONFIG_LITERAL"');
    assert.equal(
      result.warnings.some((warning) =>
        warning.includes("@workspace/tsconfig/base.json"),
      ),
      false,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("configured programs exclude imported source files outside the repository", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-external-import-"),
  );
  const outside = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-external-module-"),
  );
  try {
    const outsideSource = path.join(outside, "private.ts");
    fs.writeFileSync(
      outsideSource,
      'export function externalValue(): "PRIVATE_IMPORT_LITERAL" { return "PRIVATE_IMPORT_LITERAL"; }\n',
      "utf8",
    );
    const moduleSpecifier = path
      .relative(path.join(repo, "src"), outsideSource)
      .replaceAll("\\", "/")
      .replace(/\.ts$/, ".js");
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          moduleResolution: "Bundler",
          preserveSymlinks: true,
          target: "ES2022",
        },
        include: ["src/**/*"],
      }),
    );
    writeFile(
      repo,
      "src/changed.ts",
      [
        `import { externalValue } from ${JSON.stringify(moduleSpecifier)};`,
        "export function visible() { return externalValue(); }",
      ].join("\n"),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/changed.ts"] }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/changed.ts",
      "--range",
      "src/changed.ts:1-2",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const visible = result.files[0].symbols.find(
      (symbol) => symbol.name === "visible",
    );
    assert.equal(visible?.signature, "(): any");
    assert.equal(
      JSON.stringify(result).includes("PRIVATE_IMPORT_LITERAL"),
      false,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
});

test("compiler source reads reject a path swapped after validation", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-source-race-"),
  );
  const outside = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-source-race-target-"),
  );
  const originalRealpathSync = fs.realpathSync;
  try {
    const dependencyPath = path.join(repo, "src", "dependency.ts");
    writeFile(
      repo,
      "src/dependency.ts",
      "export function dependencyValue(): number { return 1; }\n",
    );
    writeFile(
      repo,
      "src/changed.ts",
      [
        "import { dependencyValue } from './dependency.js';",
        "export function visible() { return dependencyValue(); }",
      ].join("\n"),
    );
    const outsideSourcePath = path.join(outside, "private.ts");
    fs.writeFileSync(
      outsideSourcePath,
      'export function dependencyValue(): "PRIVATE_SOURCE_RACE" { return "PRIVATE_SOURCE_RACE"; }\n',
      "utf8",
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["src/changed.ts", "src/dependency.ts"],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];
    let swapped = false;

    fs.realpathSync = ((candidate: fs.PathLike, ...rest: unknown[]) => {
      const result = (originalRealpathSync as (...args: unknown[]) => unknown)(
        candidate,
        ...rest,
      );
      if (
        !swapped &&
        path.resolve(String(candidate)) === dependencyPath
      ) {
        swapped = true;
        fs.rmSync(dependencyPath);
        fs.symlinkSync(outsideSourcePath, dependencyPath);
      }
      return result;
    }) as typeof fs.realpathSync;

    const context = createProgramContexts(
      args,
      warnings,
      inventory,
    ).get("src/changed.ts");
    assert.ok(context);
    const source = context.program.getSourceFile(
      path.join(repo, "src", "changed.ts"),
    );
    assert.ok(source);
    const declaration = source.statements.find(
      (statement): statement is ts.FunctionDeclaration =>
        ts.isFunctionDeclaration(statement) &&
        statement.name?.text === "visible",
    );
    assert.ok(declaration);
    const signature = context.checker.getSignatureFromDeclaration(declaration);
    assert.ok(signature);
    const renderedSignature = context.checker.signatureToString(signature);

    assert.equal(swapped, true);
    assert.equal(renderedSignature.includes("PRIVATE_SOURCE_RACE"), false);
  } finally {
    fs.realpathSync = originalRealpathSync;
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
});

test("configured programs exclude imported project sources omitted from the manifest", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-excluded-import-"),
  );
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          moduleResolution: "Bundler",
          target: "ES2022",
        },
        include: ["src/**/*"],
      }),
    );
    writeFile(
      repo,
      "src/private.ts",
      'export function privateValue(): "PRIVATE_PROJECT_LITERAL" { return "PRIVATE_PROJECT_LITERAL"; }\n',
    );
    writeFile(
      repo,
      "src/changed.ts",
      [
        "import { privateValue } from './private.js';",
        "export function visible() { return privateValue(); }",
      ].join("\n"),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/changed.ts"] }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/changed.ts",
      "--range",
      "src/changed.ts:1-2",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const visible = result.files[0].symbols.find(
      (symbol) => symbol.name === "visible",
    );
    assert.equal(visible?.signature, "(): any");
    assert.equal(
      JSON.stringify(result).includes("PRIVATE_PROJECT_LITERAL"),
      false,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("manifest-bounded programs retain installed dependency declarations", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-dependency-types-"),
  );
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          moduleResolution: "Bundler",
          target: "ES2022",
        },
        include: ["src/**/*"],
      }),
    );
    writeFile(
      repo,
      "node_modules/review-dependency/package.json",
      JSON.stringify({
        name: "review-dependency",
        types: "index.d.ts",
      }),
    );
    writeFile(
      repo,
      "node_modules/review-dependency/index.d.ts",
      'export declare function dependencyValue(): "DEPENDENCY_LITERAL";\n',
    );
    writeFile(
      repo,
      "src/changed.ts",
      [
        "import { dependencyValue } from 'review-dependency';",
        "export function visible() { return dependencyValue(); }",
      ].join("\n"),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/changed.ts"] }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/changed.ts",
      "--range",
      "src/changed.ts:1-2",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const visible = result.files[0].symbols.find(
      (symbol) => symbol.name === "visible",
    );
    assert.equal(visible?.signature, '(): "DEPENDENCY_LITERAL"');
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("manifest-bounded programs reject dependency symlinks outside the repository", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-dependency-symlink-"),
  );
  const outside = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-private-dependency-"),
  );
  try {
    writeFile(
      outside,
      "package.json",
      JSON.stringify({
        name: "private-dependency",
        types: "index.d.ts",
      }),
    );
    writeFile(
      outside,
      "index.d.ts",
      'export declare function dependencyValue(): "PRIVATE_DEPENDENCY_LITERAL";\n',
    );
    fs.mkdirSync(path.join(repo, "node_modules"), { recursive: true });
    fs.symlinkSync(
      outside,
      path.join(repo, "node_modules", "private-dependency"),
      "dir",
    );
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          moduleResolution: "Bundler",
          preserveSymlinks: true,
          target: "ES2022",
        },
        include: ["src/**/*"],
      }),
    );
    writeFile(
      repo,
      "src/changed.ts",
      [
        "import { dependencyValue } from 'private-dependency';",
        "export function visible() { return dependencyValue(); }",
      ].join("\n"),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/changed.ts"] }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/changed.ts",
      "--range",
      "src/changed.ts:1-2",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const visible = result.files[0].symbols.find(
      (symbol) => symbol.name === "visible",
    );
    assert.equal(visible?.signature, "(): any");
    assert.equal(
      JSON.stringify(result).includes("PRIVATE_DEPENDENCY_LITERAL"),
      false,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outside, { recursive: true, force: true });
  }
});

test("manifest-bounded programs allow included workspace dependencies through node_modules symlinks", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-included-workspace-symlink-"),
  );
  try {
    writeFile(
      repo,
      "packages/included-dependency/package.json",
      JSON.stringify({
        name: "included-dependency",
        types: "index.d.ts",
      }),
    );
    writeFile(
      repo,
      "packages/included-dependency/index.d.ts",
      'export declare function dependencyValue(): "INCLUDED_WORKSPACE_LITERAL";\n',
    );
    fs.mkdirSync(path.join(repo, "node_modules"), { recursive: true });
    fs.symlinkSync(
      path.join(repo, "packages", "included-dependency"),
      path.join(repo, "node_modules", "included-dependency"),
      "dir",
    );
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          moduleResolution: "Bundler",
          preserveSymlinks: true,
          target: "ES2022",
        },
        include: ["src/**/*"],
      }),
    );
    writeFile(
      repo,
      "src/changed.ts",
      [
        "import { dependencyValue } from 'included-dependency';",
        "export function visible() { return dependencyValue(); }",
      ].join("\n"),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [
          "src/changed.ts",
          "packages/included-dependency/index.d.ts",
        ],
        package_files: [
          "packages/included-dependency/package.json",
        ],
      }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/changed.ts",
      "--range",
      "src/changed.ts:1-2",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const visible = result.files[0].symbols.find(
      (symbol) => symbol.name === "visible",
    );
    assert.equal(visible?.signature, '(): "INCLUDED_WORKSPACE_LITERAL"');
    assert.equal(result.partial, false);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("manifest-bounded programs reject node_modules symlinks to omitted workspace sources", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-workspace-dependency-symlink-"),
  );
  try {
    writeFile(
      repo,
      "packages/private-dependency/package.json",
      JSON.stringify({
        name: "private-dependency",
        types: "index.d.ts",
      }),
    );
    writeFile(
      repo,
      "packages/private-dependency/index.d.ts",
      'export declare function dependencyValue(): "PRIVATE_WORKSPACE_DEPENDENCY_LITERAL";\n',
    );
    fs.mkdirSync(path.join(repo, "node_modules"), { recursive: true });
    fs.symlinkSync(
      path.join(repo, "packages", "private-dependency"),
      path.join(repo, "node_modules", "private-dependency"),
      "dir",
    );
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          moduleResolution: "Bundler",
          preserveSymlinks: true,
          target: "ES2022",
        },
        include: ["src/**/*"],
      }),
    );
    writeFile(
      repo,
      "src/changed.ts",
      [
        "import { dependencyValue } from 'private-dependency';",
        "export function visible() { return dependencyValue(); }",
      ].join("\n"),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/changed.ts"] }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/changed.ts",
      "--range",
      "src/changed.ts:1-2",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const visible = result.files[0].symbols.find(
      (symbol) => symbol.name === "visible",
    );
    assert.equal(visible?.signature, "(): any");
    assert.equal(
      JSON.stringify(result).includes(
        "PRIVATE_WORKSPACE_DEPENDENCY_LITERAL",
      ),
      false,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("configured programs do not emit references from files excluded by the manifest", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-reference-manifest-"));
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: { target: "ES2022", moduleResolution: "Bundler" },
        include: ["src/**/*", "generated/**/*"],
      }),
    );
    writeFile(repo, "src/service.ts", "export function charge() { return 1; }\n");
    const excludedImports = Array.from(
      { length: 30 },
      (_, index) => `import '../generated/excluded-${String(index).padStart(2, "0")}.js';`,
    );
    writeFile(
      repo,
      "src/consumer.ts",
      [
        "import { charge } from './service.js';",
        ...excludedImports,
        "export const permittedReference = charge();",
      ].join("\n"),
    );
    writeFile(
      repo,
      "generated/root-only.ts",
      [
        "import { charge } from '../src/service.js';",
        "export const rootOnlySecret = charge();",
      ].join("\n"),
    );
    for (let index = 0; index < 30; index += 1) {
      const padded = String(index).padStart(2, "0");
      writeFile(
        repo,
        `generated/excluded-${padded}.ts`,
        [
          "import { charge } from '../src/service.js';",
          `export const corporateSecret${padded} = charge();`,
        ].join("\n"),
      );
    }
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["src/consumer.ts", "src/service.ts"],
      }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/service.ts",
      "--range",
      "src/service.ts:1-1",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const charge = result.files[0].changedSymbols.find((symbol) => symbol.name === "charge");
    assert.ok(charge);
    assert.ok(
      charge.references.some(
        (reference) =>
          reference.file === "src/consumer.ts" &&
          reference.text.includes("permittedReference = charge()"),
      ),
    );
    assert.equal(
      charge.references.some((reference) => reference.file.startsWith("generated/")),
      false,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test(
  "manifest filtering preserves references with filesystem-equivalent casing",
  { skip: ts.sys.useCaseSensitiveFileNames },
  () => {
    const repo = fs.mkdtempSync(
      path.join(os.tmpdir(), "apex-ray-ts-analyzer-reference-manifest-case-"),
    );
    try {
      writeFile(
        repo,
        "tsconfig.json",
        JSON.stringify({
          compilerOptions: {
            forceConsistentCasingInFileNames: false,
            moduleResolution: "Bundler",
            target: "ES2022",
          },
          include: ["src/**/*"],
        }),
      );
      writeFile(
        repo,
        "src/ZConsumer.ts",
        [
          "import { calculateRisk } from './service.js';",
          "export const observedRisk = calculateRisk();",
        ].join("\n"),
      );
      writeFile(
        repo,
        "src/bootstrap.ts",
        "import './zconsumer.js';\n",
      );
      writeFile(
        repo,
        "src/service.ts",
        "export function calculateRisk() { return 1; }\n",
      );
      const manifestPath = path.join(repo, "files.json");
      fs.writeFileSync(
        manifestPath,
        JSON.stringify({
          version: 2,
          files: ["src/ZConsumer.ts", "src/bootstrap.ts", "src/service.ts"],
        }),
        "utf8",
      );

      const result = runAnalyzerInProcess(repo, [
        "src/service.ts",
        "--range",
        "src/service.ts:1-1",
        "--file-manifest",
        manifestPath,
        "--no-index-cache",
      ]);

      const calculateRisk = result.files[0].changedSymbols.find(
        (symbol) => symbol.name === "calculateRisk",
      );
      assert.ok(calculateRisk);
      assert.ok(
        calculateRisk.references.some(
          (reference) =>
            reference.file.toLowerCase() === "src/zconsumer.ts" &&
            reference.text.includes("observedRisk = calculateRisk()"),
        ),
      );
    } finally {
      fs.rmSync(repo, { recursive: true, force: true });
    }
  },
);

test("manifest-excluded contracts do not consume the collection limit", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-contract-manifest-"));
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: { target: "ES2022", moduleResolution: "Bundler" },
        include: ["src/**/*", "generated/**/*"],
      }),
    );
    const excludedTypeNames = Array.from(
      { length: 24 },
      (_, index) => `Excluded${String(index).padStart(2, "0")}`,
    );
    for (const typeName of excludedTypeNames) {
      writeFile(repo, `generated/${typeName}.ts`, `export interface ${typeName} { secret: string; }\n`);
    }
    writeFile(repo, "src/permitted.ts", "export interface PermittedContract { visible: string; }\n");
    writeFile(
      repo,
      "src/service.ts",
      [
        ...excludedTypeNames.map(
          (typeName) => `import type { ${typeName} } from '../generated/${typeName}.js';`,
        ),
        "import type { PermittedContract } from './permitted.js';",
        "",
        "export function review(",
        ...excludedTypeNames.map(
          (typeName, index) => `  excluded${index}: ${typeName},`,
        ),
        "  permitted: PermittedContract,",
        "): PermittedContract {",
        "  return permitted;",
        "}",
      ].join("\n"),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/service.ts", "src/permitted.ts"] }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/service.ts",
      "--range",
      "src/service.ts:27-55",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const review = result.files[0].changedSymbols.find((symbol) => symbol.name === "review");
    assert.ok(review);
    assert.ok(review.contracts.some((reference) => reference.file === "src/permitted.ts"));
    assert.equal(
      review.contracts.some((reference) => reference.file.startsWith("generated/")),
      false,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("focused programs cap supplemental ambient declaration roots deterministically", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-focused-ambient-cap-"));
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: { target: "ES2022", strict: true },
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(
      repo,
      "src/consumer.ts",
      [
        "export function firstAmbient() { return ambient000(); }",
        "export function lastAmbient() { return ambient129(); }",
      ].join("\n"),
    );
    const declarationPaths = Array.from({ length: 130 }, (_, index) => {
      const padded = String(index).padStart(3, "0");
      const declarationPath = `src/types/ambient-${padded}.d.ts`;
      writeFile(repo, declarationPath, `declare function ambient${padded}(): number;\n`);
      return declarationPath;
    });
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: ["src/consumer.ts", ...declarationPaths] }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/consumer.ts",
      "--range",
      "src/consumer.ts:1-2",
      "--file-manifest",
      manifestPath,
      "--large-change-set-size",
      "40",
      "--no-index-cache",
    ]);

    const signatures = new Map(result.files[0].symbols.map((symbol) => [symbol.name, symbol.signature]));
    assert.equal(signatures.get("firstAmbient"), "(): number");
    assert.equal(signatures.get("lastAmbient"), "(): any");
    assert.ok(
      result.warnings.some(
        (warning) =>
          warning.includes("declaration roots capped at 128 of 130") &&
          warning.includes("ambient declaration coverage is partial"),
      ),
    );
    assert.deepEqual(result.coverage, {
      partial: true,
      reasonCodes: ["program_context_incomplete"],
      scopes: ["program_contexts"],
      failedFileCount: 0,
    });
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("supplemental declaration root selection keeps a bounded deterministic top set", () => {
  const roots = Array.from(
    { length: 10_000 },
    (_, index) => `/repo/src/types/ambient-${String(9_999 - index).padStart(4, "0")}.d.ts`,
  );
  roots.push("/repo/src/types/globals.d.ts");
  roots.push("/repo/src/types/ambient-0000.d.ts");
  const warnings: string[] = [];

  const selected = selectSupplementalDeclarationRoots(roots, [], warnings);

  assert.equal(selected.length, 128);
  assert.equal(selected[0], "/repo/src/types/globals.d.ts");
  assert.equal(new Set(selected).size, selected.length);
  assert.ok(selected.includes("/repo/src/types/ambient-0000.d.ts"));
  assert.ok(!selected.includes("/repo/src/types/ambient-9999.d.ts"));
  assert.deepEqual(warnings, [
    "TypeScript declaration roots capped at 128 of 10001; ambient declaration coverage is partial.",
  ]);
});

test("config-backed programs bound aggregate transitive source files and bytes", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-config-program-closure-"),
  );
  try {
    const changedPath = "src/consumer.ts";
    const globalPath = "src/types/global.d.ts";
    const hugePath = "src/types/generated/huge.generated.d.ts";
    const chainPaths = Array.from(
      { length: 520 },
      (_, index) => `src/types/chain/${String(index).padStart(3, "0")}.d.ts`,
    );
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: { target: "ES2022", strict: true },
        files: [changedPath],
      }),
    );
    writeFile(
      repo,
      changedPath,
      [
        '/// <reference path="./types/global.d.ts" />',
        "export function usesAmbient(): number { return focusedAmbient(); }",
      ].join("\n"),
    );
    writeFile(
      repo,
      globalPath,
      [
        '/// <reference path="./generated/huge.generated.d.ts" />',
        '/// <reference path="./chain/000.d.ts" />',
        "declare function focusedAmbient(): number;",
      ].join("\n"),
    );
    writeFile(
      repo,
      hugePath,
      `/*${"x".repeat(9 * 1024 * 1024)}*/\ndeclare const generatedHuge: true;\n`,
    );
    chainPaths.forEach((chainPath, index) => {
      const nextPath = chainPaths[index + 1];
      writeFile(
        repo,
        chainPath,
        [
          ...(nextPath
            ? [`/// <reference path="./${path.basename(nextPath)}" />`]
            : []),
          `declare const chain${index}: true;`,
        ].join("\n"),
      );
    });
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [changedPath, globalPath, hugePath, ...chainPaths],
        config_files: ["tsconfig.json"],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      changedPath,
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];

    const context = createProgramContexts(args, warnings, inventory).get(changedPath);

    assert.ok(context);
    assert.equal(inventory.partial, true);
    assert.equal(
      context.program.getSourceFile(path.join(repo, hugePath)),
      undefined,
    );
    const repoSources = context.program
      .getSourceFiles()
      .filter(
        (sourceFile) =>
          sourceFile.fileName.startsWith(`${repo}${path.sep}`),
      );
    assert.ok(repoSources.length <= 512);
    assert.ok(
      warnings.some(
        (warning) =>
          warning.includes("program source budget reached") &&
          warning.includes("compiler context is partial"),
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("program source budget is shared across config groups and reserves every changed root first", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-shared-program-budget-"),
  );
  try {
    const firstChangedPath = "packages/a/src/changed.ts";
    const firstDependencyPath = "packages/a/src/large-dependency.ts";
    const secondChangedPath = "packages/b/src/changed.ts";
    for (const packageName of ["a", "b"]) {
      writeFile(
        repo,
        `packages/${packageName}/tsconfig.json`,
        JSON.stringify({
          compilerOptions: {
            module: "ESNext",
            moduleResolution: "Bundler",
            target: "ES2022",
          },
          files: ["src/changed.ts"],
        }),
      );
    }
    writeFile(
      repo,
      firstChangedPath,
      [
        'import "./large-dependency.js";',
        "export const firstChanged = true;",
      ].join("\n"),
    );
    writeFile(
      repo,
      firstDependencyPath,
      `/*${"x".repeat(15 * 512 * 1024)}*/\nexport const largeDependency = true;\n`,
    );
    writeFile(
      repo,
      secondChangedPath,
      `/*${"y".repeat(1024 * 1024)}*/\nexport const secondChanged = true;\n`,
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [
          firstChangedPath,
          firstDependencyPath,
          secondChangedPath,
        ],
        config_files: [
          "packages/a/tsconfig.json",
          "packages/b/tsconfig.json",
        ],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      firstChangedPath,
      secondChangedPath,
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];

    const contexts = createProgramContexts(args, warnings, inventory);
    const firstContext = contexts.get(firstChangedPath);
    const secondContext = contexts.get(secondChangedPath);

    assert.ok(firstContext);
    assert.ok(secondContext);
    assert.ok(
      firstContext.program.getSourceFile(path.join(repo, firstChangedPath)),
    );
    assert.ok(
      secondContext.program.getSourceFile(path.join(repo, secondChangedPath)),
    );
    assert.equal(
      firstContext.program.getSourceFile(path.join(repo, firstDependencyPath)),
      undefined,
    );
    const programs = [
      ...new Set(
        [...contexts.values()].map((context) => context.program),
      ),
    ];
    const retainedRepoSourceBytes = programs
      .flatMap((program) => program.getSourceFiles())
      .filter((sourceFile) =>
        sourceFile.fileName.startsWith(`${repo}${path.sep}`),
      )
      .reduce(
        (total, sourceFile) =>
          total + Buffer.byteLength(sourceFile.text, "utf8"),
        0,
      );
    assert.ok(retainedRepoSourceBytes <= 8 * 1024 * 1024);
    assert.equal(inventory.partial, true);
    assert.ok(
      warnings.some((warning) =>
        warning.includes("program source budget reached"),
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("compiler config metadata is rejected before an oversized read and reported as partial", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-config-metadata-budget-"),
  );
  const originalReadFileSync = fs.readFileSync;
  try {
    const changedPath = "src/changed.ts";
    writeFile(repo, changedPath, "export const changed = true;\n");
    writeFile(
      repo,
      "tsconfig.json",
      `${" ".repeat(4 * 1024 * 1024 + 1)}${JSON.stringify({
        compilerOptions: { target: "ES2022" },
        files: [changedPath],
      })}`,
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [changedPath],
        config_files: ["tsconfig.json"],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      changedPath,
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];
    let oversizedReadAttempted = false;
    fs.readFileSync = ((
      candidate: fs.PathOrFileDescriptor,
      ...rest: unknown[]
    ) => {
      if (
        typeof candidate === "number" &&
        fs.fstatSync(candidate).size > 4 * 1024 * 1024
      ) {
        oversizedReadAttempted = true;
      }
      return (originalReadFileSync as (...args: unknown[]) => string | Buffer)(
        candidate,
        ...rest,
      );
    }) as typeof fs.readFileSync;

    const context = createProgramContexts(
      args,
      warnings,
      inventory,
    ).get(changedPath);

    assert.ok(context);
    assert.equal(context.tsconfigPath, null);
    assert.equal(oversizedReadAttempted, false);
    assert.equal(inventory.configurationPartial, true);
    assert.ok(
      warnings.some(
        (warning) =>
          warning.includes("compiler metadata byte safety limit") &&
          warning.includes("configuration context is partial"),
      ),
    );
  } finally {
    fs.readFileSync = originalReadFileSync;
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("workspace package metadata is bounded before resolving config extends", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-package-metadata-budget-"),
  );
  const originalReadFileSync = fs.readFileSync;
  try {
    const changedPath = "apps/web/src/changed.ts";
    writeFile(repo, changedPath, "export const changed = true;\n");
    writeFile(
      repo,
      "apps/web/tsconfig.json",
      JSON.stringify({
        extends: "@workspace/config",
        files: ["src/changed.ts"],
      }),
    );
    writeFile(
      repo,
      "packages/config/package.json",
      `${" ".repeat(4 * 1024 * 1024 + 1)}${JSON.stringify({
        name: "@workspace/config",
        tsconfig: "base.json",
      })}`,
    );
    writeFile(
      repo,
      "packages/config/base.json",
      JSON.stringify({ compilerOptions: { target: "ES2022" } }),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [changedPath],
        package_files: ["packages/config/package.json"],
        config_files: [
          "apps/web/tsconfig.json",
          "packages/config/base.json",
        ],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      changedPath,
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];
    let oversizedReadAttempted = false;
    fs.readFileSync = ((
      candidate: fs.PathOrFileDescriptor,
      ...rest: unknown[]
    ) => {
      if (
        typeof candidate === "number" &&
        fs.fstatSync(candidate).size > 4 * 1024 * 1024
      ) {
        oversizedReadAttempted = true;
      }
      return (originalReadFileSync as (...args: unknown[]) => string | Buffer)(
        candidate,
        ...rest,
      );
    }) as typeof fs.readFileSync;

    createProgramContexts(args, warnings, inventory);

    assert.equal(oversizedReadAttempted, false);
    assert.equal(inventory.configurationPartial, true);
    assert.ok(
      warnings.some((warning) =>
        warning.includes("compiler metadata byte safety limit"),
      ),
    );
  } finally {
    fs.readFileSync = originalReadFileSync;
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("workspace package export amplification marks configuration partial", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-package-export-budget-"),
  );
  try {
    const changedPath = "apps/web/src/changed.ts";
    const wildcard = "x".repeat(8_192);
    writeFile(repo, changedPath, "export const changed = true;\n");
    writeFile(
      repo,
      "apps/web/tsconfig.json",
      JSON.stringify({
        extends: `@workspace/config/${wildcard}`,
        files: ["src/changed.ts"],
      }),
    );
    writeFile(
      repo,
      "packages/config/package.json",
      JSON.stringify({
        name: "@workspace/config",
        exports: {
          "./*": "*".repeat(8_192),
        },
      }),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [changedPath],
        package_files: ["packages/config/package.json"],
        config_files: ["apps/web/tsconfig.json"],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      changedPath,
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];

    createProgramContexts(args, warnings, inventory);

    assert.equal(inventory.configurationPartial, true);
    assert.ok(
      warnings.some(
        (warning) =>
          warning.includes(
            "package export target expansion safety limit",
          ) &&
          warning.includes("configuration context is partial"),
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("late package resolution limits mark analysis partial and suppress its cache", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-package-resolution-budget-"),
  );
  const cacheDir = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-package-resolution-cache-"),
  );
  try {
    const changedPath = "packages/lib/src/target.ts";
    const testPath = "tests/consumer.test.ts";
    writeFile(repo, changedPath, "export const target = true;\n");
    writeFile(
      repo,
      testPath,
      'import { target } from "@workspace/lib/feature";\nvoid target;\n',
    );
    writeFile(
      repo,
      "packages/lib/package.json",
      JSON.stringify({
        name: "@workspace/lib",
        exports: {
          "./feature": [
            ...Array.from(
              { length: 512 },
              (_, index) => `./src/other-${index}.ts`,
            ),
            "./src/target.ts",
          ],
        },
      }),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [changedPath, testPath],
        package_files: ["packages/lib/package.json"],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      changedPath,
      "--file-manifest",
      manifestPath,
      "--index-cache-dir",
      cacheDir,
    ]);

    const result = analyze(args);

    assert.equal(result.partial, true);
    assert.equal(result.indexCache?.written, false);
    assert.equal(
      fs.existsSync(repoIndexCachePath(repo, cacheDir)),
      false,
    );
    assert.ok(
      result.warnings.some(
        (warning) =>
          warning.includes("module target expansion safety limit") &&
          warning.includes(
            "workspace references and related tests are partial",
          ),
      ),
    );
    assert.deepEqual(result.files[0]?.relatedTests, []);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("compiler metadata uses one aggregate budget across config groups", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-config-aggregate-budget-"),
  );
  const originalReadFileSync = fs.readFileSync;
  try {
    const changedPaths = [
      "packages/a/src/changed.ts",
      "packages/b/src/changed.ts",
    ];
    for (const changedPath of changedPaths) {
      writeFile(repo, changedPath, "export const changed = true;\n");
    }
    for (const packageName of ["a", "b"]) {
      writeFile(
        repo,
        `packages/${packageName}/tsconfig.json`,
        `${" ".repeat(3 * 1024 * 1024)}${JSON.stringify({
          files: ["src/changed.ts"],
        })}`,
      );
    }
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: changedPaths,
        config_files: [
          "packages/a/tsconfig.json",
          "packages/b/tsconfig.json",
        ],
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      ...changedPaths,
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);
    const inventory = loadRepoFileInventory(args);
    const warnings: string[] = [];
    const firstConfigIno = fs.statSync(
      path.join(repo, "packages/a/tsconfig.json"),
    ).ino;
    const secondConfigIno = fs.statSync(
      path.join(repo, "packages/b/tsconfig.json"),
    ).ino;
    let firstConfigReads = 0;
    let secondConfigReads = 0;
    fs.readFileSync = ((
      candidate: fs.PathOrFileDescriptor,
      ...rest: unknown[]
    ) => {
      if (
        typeof candidate === "number" &&
        fs.fstatSync(candidate).size > 2 * 1024 * 1024
      ) {
        const fileIno = fs.fstatSync(candidate).ino;
        if (fileIno === firstConfigIno) firstConfigReads += 1;
        if (fileIno === secondConfigIno) secondConfigReads += 1;
      }
      return (originalReadFileSync as (...args: unknown[]) => string | Buffer)(
        candidate,
        ...rest,
      );
    }) as typeof fs.readFileSync;

    const contexts = createProgramContexts(args, warnings, inventory);

    assert.ok(firstConfigReads >= 1);
    assert.equal(secondConfigReads, 0);
    assert.equal(
      contexts.get(changedPaths[0])?.tsconfigPath,
      path.join(repo, "packages/a/tsconfig.json"),
    );
    assert.equal(contexts.get(changedPaths[1])?.tsconfigPath, null);
    assert.equal(inventory.configurationPartial, true);
    assert.ok(
      warnings.some(
        (warning) =>
          warning.includes("compiler metadata byte safety limit") &&
          warning.includes("in aggregate"),
      ),
    );
  } finally {
    fs.readFileSync = originalReadFileSync;
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("config-backed programs reject oversized changed roots as partial failures", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-oversized-changed-root-"),
  );
  try {
    const changedPath = "src/huge-changed.ts";
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: { target: "ES2022", strict: true },
        files: [changedPath],
      }),
    );
    writeFile(
      repo,
      changedPath,
      `/*${"x".repeat(9 * 1024 * 1024)}*/\nexport const changed = true;\n`,
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [changedPath],
        config_files: ["tsconfig.json"],
      }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      changedPath,
      "--range",
      `${changedPath}:2-2`,
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    assert.equal(result.partial, true);
    assert.deepEqual(result.files, []);
    assert.deepEqual(result.failedFiles, [changedPath]);
    assert.ok(
      result.warnings.some(
        (warning) =>
          warning.includes("program source budget reached") &&
          warning.includes("compiler context is partial"),
      ),
    );
    assert.ok(result.coverage.reasonCodes.includes("program_context_incomplete"));
    assert.ok(result.coverage.reasonCodes.includes("changed_file_analysis_incomplete"));
    assert.equal(result.coverage.reasonCodes.includes("repository_inventory_partial"), false);
    assert.ok(result.coverage.reasonCodes.includes("workspace_index_partial"));
    assert.ok(result.coverage.scopes.includes("program_contexts"));
    assert.equal(result.coverage.scopes.includes("repository_inventory"), false);
    assert.ok(result.coverage.scopes.includes("workspace_index"));
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("manifest ingestion rejects over-limit arrays before retaining entries", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-manifest-entry-ingestion-"),
  );
  try {
    const changedPath = "src/changed.ts";
    writeFile(repo, changedPath, "export const changed = true;\n");
    const supplementalPaths = Array.from(
      { length: 4 },
      (_, index) => `src/supplemental-${index}.ts`,
    );
    for (const supplementalPath of supplementalPaths) {
      writeFile(repo, supplementalPath, "export const supplemental = true;\n");
    }
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: supplementalPaths }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      changedPath,
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const inventory = loadRepoFileInventory(args, { maxFiles: 3 });

    assert.deepEqual(inventory.absPaths, [path.join(repo, changedPath)]);
    assert.equal(inventory.partial, true);
    assert.ok(inventory.partialReason?.includes("manifest entry safety limit"));
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("fallback traversal enforces its retained-path byte budget", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-fallback-path-bytes-"),
  );
  try {
    const changedPath = "changed.ts";
    writeFile(repo, changedPath, "export const changed = true;\n");
    writeFile(repo, "src/extra.ts", "export const extra = true;\n");
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      changedPath,
      "--no-index-cache",
    ]);

    const inventory = loadRepoFileInventory(args, { maxPathBytes: 1 });

    assert.deepEqual(inventory.absPaths, [path.join(repo, changedPath)]);
    assert.equal(inventory.partial, true);
    assert.ok(
      inventory.partialReason?.includes(
        "fallback inventory scan reached the retained-path byte safety limit",
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("analyzer exposes repo index cache write failures as warnings", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-cache-warning-"));
  const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-cache-warning-home-"));
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: { target: "ES2022" },
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(repo, "src/cart.ts", "export const cart = true;\n");
    fs.mkdirSync(repoIndexCachePath(repo, cacheDir));

    const result = runAnalyzerInProcess(repo, [
      "src/cart.ts",
      "--range",
      "src/cart.ts:1-1",
      "--index-cache-dir",
      cacheDir,
    ]);

    assert.equal(result.indexCache?.written, false);
    assert.ok(result.warnings.some((warning) => warning.includes("repo index cache") && warning.includes("could not be written")));
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("analyzer returns partial JSON when the internal budget is exhausted", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-budget-"));
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
        },
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(repo, "src/first.ts", "export function first(): number {\n  return 1;\n}\n");
    writeFile(repo, "src/second.ts", "export function second(): number {\n  return 2;\n}\n");

    const result = runAnalyzer(repo, [
      "src/first.ts",
      "src/second.ts",
      "--range",
      "src/first.ts:1-3",
      "--range",
      "src/second.ts:1-3",
      "--analysis-time-budget-ms",
      "0",
      "--no-index-cache",
    ]);

    assert.equal(result.partial, true);
    assert.deepEqual(result.files, []);
    assert.deepEqual(result.failedFiles, ["src/first.ts", "src/second.ts"]);
    assert.equal(result.shardFailures.length, 1);
    assert.equal(result.shardFailures[0].status, "timeout");
    assert.deepEqual(result.shardFailures[0].files, ["src/first.ts", "src/second.ts"]);
    assert.ok(
      result.warnings.some((warning) =>
        warning.includes("fallback inventory scan stopped because the analysis time budget was exhausted"),
      ),
    );
    assert.ok(result.warnings.some((warning) => warning.includes("internal budget exhausted")));
    assert.equal(result.coverage.partial, true);
    assert.ok(result.coverage.reasonCodes.includes("analysis_time_budget_exhausted"));
    assert.ok(result.coverage.reasonCodes.includes("changed_file_analysis_incomplete"));
    assert.ok(result.coverage.scopes.includes("analyzer"));
    assert.ok(result.coverage.scopes.includes("changed_files"));
    assert.equal(result.coverage.failedFileCount, 2);
    assert.equal(result.metrics.shards[0].status, "timeout");
    assert.ok(result.metrics.wallDurationMs >= 0);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("analyzer marks missing changed source files as failed and partial", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-missing-changed-"),
  );
  try {
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 2, files: [] }),
      "utf8",
    );

    const result = runAnalyzerInProcess(repo, [
      "src/missing.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    assert.equal(result.partial, true);
    assert.deepEqual(result.failedFiles, ["src/missing.ts"]);
    assert.ok(
      result.shardFailures.some(
        (failure) =>
          failure.status === "failed" &&
          failure.files.includes("src/missing.ts"),
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("manifest producer partial reason preserves changed-file inventory", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-producer-partial-"),
  );
  try {
    writeFile(repo, "src/changed.ts", "export const changed = true;\n");
    const manifestPath = path.join(repo, "files.json");
    const partialReason =
      "TypeScript file manifest producer reached a safety limit; repository context is partial.";
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [],
        package_files: [],
        config_files: [],
        partial_reason: partialReason,
      }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/changed.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    const inventory = loadRepoFileInventory(args);

    assert.equal(inventory.partial, true);
    assert.equal(inventory.fingerprint, null);
    assert.equal(inventory.partialReason, partialReason);
    assert.ok(
      inventory.absPaths.includes(path.join(repo, "src", "changed.ts")),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("compiler source loading stops immediately when the analysis budget expires", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-compiler-budget-"),
  );
  const originalNow = Date.now;
  const originalOpenSync = fs.openSync;
  try {
    const changedPath = path.join(repo, "src", "changed.ts");
    const dependencyPath = path.join(repo, "src", "dependency.ts");
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({ files: ["src/changed.ts"] }),
    );
    writeFile(
      repo,
      "src/changed.ts",
      [
        "import { dependencyValue } from './dependency.js';",
        "export const visible = dependencyValue;",
      ].join("\n"),
    );
    writeFile(
      repo,
      "src/dependency.ts",
      "export const dependencyValue = 1;\n",
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: ["src/changed.ts", "src/dependency.ts"],
        config_files: ["tsconfig.json"],
      }),
      "utf8",
    );
    let fakeNow = 0;
    const openedPaths: string[] = [];
    Date.now = () => fakeNow;
    fs.openSync = ((candidate: fs.PathLike, ...rest: unknown[]) => {
      const descriptor = (originalOpenSync as (...args: unknown[]) => number)(
        candidate,
        ...rest,
      );
      const resolved = path.resolve(String(candidate));
      openedPaths.push(resolved);
      if (resolved === changedPath) fakeNow = 1;
      return descriptor;
    }) as typeof fs.openSync;

    const result = runAnalyzerInProcess(repo, [
      "src/changed.ts",
      "--range",
      "src/changed.ts:1-2",
      "--file-manifest",
      manifestPath,
      "--analysis-time-budget-ms",
      "1",
      "--no-index-cache",
    ]);

    assert.equal(result.partial, true);
    assert.deepEqual(result.files, []);
    assert.deepEqual(result.failedFiles, ["src/changed.ts"]);
    assert.ok(
      result.shardFailures.some(
        (failure) => failure.status === "timeout",
      ),
    );
    assert.ok(
      result.warnings.some((warning) =>
        warning.includes("internal budget exhausted"),
      ),
    );
    assert.ok(openedPaths.includes(changedPath));
    assert.equal(openedPaths.includes(dependencyPath), false);
  } finally {
    Date.now = originalNow;
    fs.openSync = originalOpenSync;
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("compiler stack failures return partial analyzer JSON", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-compiler-stack-"),
  );
  try {
    const chainLength = 800;
    const chainPaths = Array.from(
      { length: chainLength },
      (_, index) => `src/chain-${String(index).padStart(4, "0")}.ts`,
    );
    for (let index = 0; index < chainPaths.length; index += 1) {
      const nextPath = chainPaths[index + 1];
      const content = nextPath
        ? [
            `import { value as nextValue } from './${path.basename(nextPath, ".ts")}.js';`,
            "export const value = nextValue + 1;",
          ].join("\n")
        : "export const value = 1;\n";
      writeFile(repo, chainPaths[index], content);
    }
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({ files: [chainPaths[0]] }),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: chainPaths,
        config_files: ["tsconfig.json"],
      }),
      "utf8",
    );

    const stdout = execFileSync(
      process.execPath,
      [
        "--stack-size=256",
        analyzerScript,
        "--repo",
        repo,
        "--changed",
        chainPaths[0],
        "--range",
        `${chainPaths[0]}:1-2`,
        "--file-manifest",
        manifestPath,
        "--analysis-time-budget-ms",
        "120000",
        "--no-index-cache",
      ],
      { encoding: "utf8", timeout: 30_000 },
    );
    const result = JSON.parse(stdout) as AnalyzerResult;

    assert.equal(result.partial, true);
    assert.deepEqual(result.files, []);
    assert.deepEqual(result.failedFiles, [chainPaths[0]]);
    assert.ok(
      result.shardFailures.some(
        (failure) => failure.status === "failed",
      ),
    );
    assert.ok(
      result.warnings.some((warning) =>
        warning.includes("TypeScript compiler could not create a program"),
      ),
    );
    assert.ok(
      result.warnings.some((warning) =>
        warning.includes("No TypeScript program could be created"),
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("repo-index parser stack failures return partial analyzer JSON", () => {
  const repo = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-analyzer-index-stack-"),
  );
  try {
    const sourcePath = "src/deep.ts";
    const nestingDepth = 1_200;
    writeFile(
      repo,
      sourcePath,
      `export const deep = ${"(".repeat(nestingDepth)}1${")".repeat(nestingDepth)};\n`,
    );
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({ files: [sourcePath] }),
    );
    const manifestPath = path.join(repo, "files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({
        version: 2,
        files: [sourcePath],
        config_files: ["tsconfig.json"],
      }),
      "utf8",
    );

    const stdout = execFileSync(
      process.execPath,
      [
        "--stack-size=256",
        analyzerScript,
        "--repo",
        repo,
        "--changed",
        sourcePath,
        "--range",
        `${sourcePath}:1-1`,
        "--file-manifest",
        manifestPath,
        "--analysis-time-budget-ms",
        "120000",
        "--no-index-cache",
      ],
      { encoding: "utf8", timeout: 30_000 },
    );
    const result = JSON.parse(stdout) as AnalyzerResult;

    assert.equal(result.partial, true);
    assert.deepEqual(result.files, []);
    assert.deepEqual(result.failedFiles, [sourcePath]);
    assert.ok(
      result.warnings.some((warning) =>
        warning.includes("could not be indexed safely"),
      ),
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("analyzer marks partial when budget expires after metadata collection", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-metadata-budget-"));
  const originalNow = Date.now;
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          experimentalDecorators: true,
          strict: true,
        },
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(
      repo,
      "src/controller.ts",
      [
        "function Controller(): ClassDecorator { return () => undefined; }",
        "function Get(): MethodDecorator { return () => undefined; }",
        "@Controller()",
        "export class CartController {",
        "  @Get()",
        "  list(): string { return 'ok'; }",
        "}",
      ].join("\n"),
    );

    let nowCalls = 0;
    Date.now = () => {
      nowCalls += 1;
      return nowCalls <= 238 ? 0 : 1;
    };

    const result = runAnalyzerInProcess(repo, [
      "src/controller.ts",
      "--range",
      "src/controller.ts:4-7",
      "--analysis-time-budget-ms",
      "1",
      "--no-index-cache",
    ]);

    assert.equal(result.partial, true);
    assert.deepEqual(result.files, []);
    assert.deepEqual(result.failedFiles, ["src/controller.ts"]);
    assert.ok(result.warnings.some((warning) => warning.includes("internal budget exhausted")));
  } finally {
    Date.now = originalNow;
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("analyzer marks partial when budget expires after collecting a file with no changed symbols", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-analyzer-empty-symbol-budget-"));
  const originalNow = Date.now;
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
        },
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(repo, "src/notes.ts", "export function stable(): number {\n  return 1;\n}\n\n// changed comment\n");

    let nowCalls = 0;
    Date.now = () => {
      nowCalls += 1;
      return nowCalls <= 2 ? 0 : 1;
    };

    const result = runAnalyzerInProcess(repo, [
      "src/notes.ts",
      "--range",
      "src/notes.ts:5-5",
      "--analysis-time-budget-ms",
      "1",
      "--no-index-cache",
    ]);

    assert.equal(result.partial, true);
    assert.deepEqual(result.files, []);
    assert.deepEqual(result.failedFiles, ["src/notes.ts"]);
    assert.ok(result.warnings.some((warning) => warning.includes("internal budget exhausted")));
  } finally {
    Date.now = originalNow;
    fs.rmSync(repo, { recursive: true, force: true });
  }
});
