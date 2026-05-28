import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { analyze } from "../dist/analyzer.js";
import { parseArgs } from "../dist/cli.js";
import { findNearestConfig, normalizeTsConfigExtends } from "../dist/program.js";
import { readRepoIndexCache, repoIndexCachePath, writeRepoIndexCache } from "../dist/repo-index-cache.js";
import type { AnalyzerResult, RepoFileIndexEntry } from "../dist/types.js";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const analyzerRoot = path.resolve(testDir, "..");
const analyzerScript = path.join(analyzerRoot, "dist", "analyze.js");

function writeFile(root: string, relativePath: string, content: string): void {
  const target = path.join(root, relativePath);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content, "utf8");
}

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

test("parseArgs normalizes analyzer CLI options", () => {
  const repo = path.join(os.tmpdir(), "apex-ray-parse-args");

  const args = parseArgs([
    "--repo",
    repo,
    "--changed",
    "src\\cart.ts",
    "src/service.ts",
    "--range",
    "src\\cart.ts:2-4",
    "--deleted-line",
    "src\\cart.ts",
    "3",
    "export function oldTotal() {}",
    "--no-index-cache",
    "--index-cache-dir",
    ".cache/typescript",
    "--refresh-index-cache",
    "--large-change-set-size",
    "12",
  ]);

  assert.equal(args.repo, path.resolve(repo));
  assert.deepEqual(args.changed, ["src/cart.ts", "src/service.ts"]);
  assert.deepEqual(args.changedRanges.get("src/cart.ts"), [[2, 4]]);
  assert.deepEqual(args.deletedLines.get("src/cart.ts"), [{ line: 3, text: "export function oldTotal() {}" }]);
  assert.equal(args.indexCacheEnabled, false);
  assert.equal(args.indexCacheDir, ".cache/typescript");
  assert.equal(args.refreshIndexCache, true);
  assert.equal(args.largeChangeSetSize, 12);
});

test("program helpers resolve nearest config and workspace package extends", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-program-"));
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
          strict: true,
        },
      }),
    );
    writeFile(
      repo,
      "apps/web/tsconfig.json",
      JSON.stringify({
        extends: "@workspace/tsconfig/base.json",
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(repo, "apps/web/src/cart.ts", "export const total = 1;\n");

    const configPath = findNearestConfig(repo, "apps/web/src/cart.ts");
    assert.equal(configPath, path.join(repo, "apps/web/tsconfig.json"));

    const normalized = normalizeTsConfigExtends(repo, configPath, {
      extends: "@workspace/tsconfig/base.json",
    });
    assert.deepEqual(normalized, {
      extends: path.join(repo, "packages/tsconfig/base.json"),
    });
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("repo index cache writes valid payloads and rejects invalid payloads", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-cache-repo-"));
  const cacheHome = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-cache-home-"));
  const previousCacheHome = process.env.APEX_RAY_CACHE_HOME;
  try {
    process.env.APEX_RAY_CACHE_HOME = cacheHome;
    const cachePath = repoIndexCachePath(repo, null);
    const file: RepoFileIndexEntry = {
      absPath: path.join(repo, "src/cart.ts"),
      relPath: "src/cart.ts",
      relLower: "src/cart.ts",
      size: 10,
      mtimeMs: 123,
      imports: [],
      exports: [],
      identifiers: [],
      receivers: [],
      typeAliases: [],
      classHeritages: [],
      diProviders: [],
      diInjections: [],
    };

    assert.equal(writeRepoIndexCache(cachePath, [file]), true);
    const parsed = readRepoIndexCache(cachePath);
    assert.ok(parsed);
    assert.equal(parsed.files[0].relPath, "src/cart.ts");

    fs.writeFileSync(cachePath, JSON.stringify({ version: -1, files: [file] }), "utf8");
    assert.equal(readRepoIndexCache(cachePath), null);
  } finally {
    if (previousCacheHome === undefined) {
      delete process.env.APEX_RAY_CACHE_HOME;
    } else {
      process.env.APEX_RAY_CACHE_HOME = previousCacheHome;
    }
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheHome, { recursive: true, force: true });
  }
});

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
