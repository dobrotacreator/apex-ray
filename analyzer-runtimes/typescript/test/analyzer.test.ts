import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { analyze } from "../dist/analyzer.js";
import { parseArgs } from "../dist/cli.js";
import { repoIndexCachePath } from "../dist/indexes/repo-cache.js";
import type { AnalyzerResult } from "../dist/types.js";
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

    assert.deepEqual(apiResult, cliResult);
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
      JSON.stringify({ version: 1, files: ["src/consumer.ts", "src/globals.d.mts"] }),
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
        version: 1,
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
        version: 1,
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
        version: 1,
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
      JSON.stringify({ version: 1, files: ["src/consumer.ts", "src/globals.d.cts"] }),
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
        version: 1,
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
      JSON.stringify({ version: 1, files: ["src/service.ts", "src/permitted.ts"] }),
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
      JSON.stringify({ version: 1, files: ["src/consumer.ts", ...declarationPaths] }),
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
    assert.ok(result.warnings.some((warning) => warning.includes("internal budget exhausted")));
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
