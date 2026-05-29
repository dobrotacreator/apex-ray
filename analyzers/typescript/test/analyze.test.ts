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
import { collectFrameworkMetadata, collectSchemaContracts } from "../dist/contract-analysis.js";
import {
  findIndexedPackageForFile,
  isModuleSpecifierRelatedToPath,
  moduleSpecifierCandidatePaths,
} from "../dist/module-resolution.js";
import { findNearestConfig, normalizeTsConfigExtends } from "../dist/program.js";
import {
  collectCallees,
  collectImplementedMemberUsageReferences,
  collectReferenceConsumerImpact,
  collectReferences,
} from "../dist/reference-analysis.js";
import { addReference, mergeReferences } from "../dist/reference-merge.js";
import { buildRepoIndex } from "../dist/repo-index.js";
import { readRepoIndexCache, repoIndexCachePath, writeRepoIndexCache } from "../dist/repo-index-cache.js";
import {
  collectDeletedSymbols,
  collectExports,
  collectImports,
  collectSymbols,
  preferSyntheticChildSymbols,
} from "../dist/symbol-collection.js";
import { findRelatedTests, isTestPath } from "../dist/test-discovery.js";
import type { AnalyzerResult, PackageInfo, Reference, RepoFileIndexEntry } from "../dist/types.js";
import {
  collectWorkspaceImportReferences,
  collectWorkspaceMemberReferences,
  filterInvalidWorkspaceMemberReferences,
} from "../dist/workspace-references.js";

const testDir = path.dirname(fileURLToPath(import.meta.url));
const analyzerRoot = path.resolve(testDir, "..");
const analyzerScript = path.join(analyzerRoot, "dist", "analyze.js");

function writeFile(root: string, relativePath: string, content: string): void {
  const target = path.join(root, relativePath);
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, content, "utf8");
}

function normalizePath(value: string): string {
  return value.replaceAll("\\", "/");
}

function assertIncludesPath(candidates: string[], expectedPath: string): void {
  const normalized = normalizePath(path.resolve(expectedPath));
  assert.ok(candidates.includes(normalized), `Expected ${normalized} in ${JSON.stringify(candidates)}`);
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

test("module resolution expands relative imports and tsconfig path aliases", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-module-resolution-"));
  try {
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          baseUrl: ".",
          paths: {
            "@app/*": ["src/*"],
            "@exact": ["src/exact.ts"],
          },
        },
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(repo, "src/features/cart.ts", "export const cart = 1;\n");
    writeFile(repo, "src/shared.ts", "export const shared = 1;\n");
    writeFile(repo, "src/exact.ts", "export const exact = 1;\n");

    const importerPath = path.join(repo, "src/features/cart.ts");

    assertIncludesPath(
      moduleSpecifierCandidatePaths("../shared.js", importerPath, repo, null),
      path.join(repo, "src/shared.ts"),
    );
    assertIncludesPath(
      moduleSpecifierCandidatePaths("@app/shared", importerPath, repo, null),
      path.join(repo, "src/shared.ts"),
    );
    assertIncludesPath(
      moduleSpecifierCandidatePaths("@exact", importerPath, repo, null),
      path.join(repo, "src/exact.ts"),
    );
    assert.equal(
      isModuleSpecifierRelatedToPath("../shared.js", importerPath, path.join(repo, "src/shared.ts"), null),
      true,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("module resolution expands workspace package root, subpath, and wildcard exports", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-package-resolution-"));
  try {
    const packageRoot = path.join(repo, "packages/lib");
    writeFile(
      repo,
      "packages/lib/package.json",
      JSON.stringify({
        name: "@acme/lib",
        exports: {
          ".": {
            types: "./src/index.ts",
          },
          "./feature": {
            import: "./src/feature.ts",
          },
          "./wild/*": "./src/wild/*.ts",
        },
      }),
    );
    writeFile(repo, "packages/lib/src/index.ts", "export const root = 1;\n");
    writeFile(repo, "packages/lib/src/feature.ts", "export const feature = 1;\n");
    writeFile(repo, "packages/lib/src/wild/button.ts", "export const button = 1;\n");
    writeFile(repo, "apps/web/src/use.ts", "export const use = 1;\n");

    const packageInfo: PackageInfo = {
      root: packageRoot,
      name: "@acme/lib",
      exports: {
        ".": {
          types: "./src/index.ts",
        },
        "./feature": {
          import: "./src/feature.ts",
        },
        "./wild/*": "./src/wild/*.ts",
      },
      main: null,
      module: null,
      types: null,
      typings: null,
    };
    const importerPath = path.join(repo, "apps/web/src/use.ts");

    assertIncludesPath(
      moduleSpecifierCandidatePaths("@acme/lib", importerPath, repo, packageInfo),
      path.join(packageRoot, "src/index.ts"),
    );
    assertIncludesPath(
      moduleSpecifierCandidatePaths("@acme/lib/feature", importerPath, repo, packageInfo),
      path.join(packageRoot, "src/feature.ts"),
    );
    assertIncludesPath(
      moduleSpecifierCandidatePaths("@acme/lib/wild/button", importerPath, repo, packageInfo),
      path.join(packageRoot, "src/wild/button.ts"),
    );
    assert.equal(
      isModuleSpecifierRelatedToPath(
        "@acme/lib/feature",
        importerPath,
        path.join(packageRoot, "src/feature.ts"),
        packageInfo,
      ),
      true,
    );
    assert.equal(
      isModuleSpecifierRelatedToPath(
        "@acme/lib/feature",
        importerPath,
        path.join(packageRoot, "src/other.ts"),
        packageInfo,
      ),
      false,
    );

    const repoIndex = buildRepoIndex(
      parseArgs(["--repo", repo, "--changed", "packages/lib/src/index.ts", "--no-index-cache"]),
    );
    const indexedPackage = findIndexedPackageForFile(repo, repoIndex, path.join(packageRoot, "src/index.ts"));
    assert.equal(indexedPackage?.name, "@acme/lib");
    assert.equal(
      findIndexedPackageForFile(repo, repoIndex, path.join(packageRoot, "src/index.ts")),
      indexedPackage,
    );
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("test discovery finds related runnable tests", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-test-discovery-"));
  try {
    writeFile(
      repo,
      "vitest.config.ts",
      [
        "export default {",
        "  test: {",
        "    include: ['src/**/*.test.ts', 'tests/**/*.spec.ts'],",
        "    exclude: ['tests/excluded.spec.ts'],",
        "  },",
        "};",
      ].join("\n"),
    );
    writeFile(repo, "src/cart.ts", "export class CartService {}\n");
    writeFile(repo, "src/cart.test.ts", "import { CartService } from './cart.js';\nnew CartService();\n");
    writeFile(
      repo,
      "src/checkout.ts",
      "import { CartService } from './cart.js';\nexport function checkout() { return new CartService(); }\n",
    );
    writeFile(repo, "tests/checkout.spec.ts", "import { checkout } from '../src/checkout.js';\ncheckout();\n");
    writeFile(repo, "tests/excluded.spec.ts", "import { checkout } from '../src/checkout.js';\ncheckout();\n");
    writeFile(repo, "e2e/cart.spec.ts", "import { CartService } from '../src/cart.js';\nnew CartService();\n");

    const repoIndex = buildRepoIndex(parseArgs(["--repo", repo, "--changed", "src/cart.ts", "--no-index-cache"]));
    const references: Reference[] = [
      { file: "src/checkout.ts", line: 2, text: "export function checkout() { return new CartService(); }", kind: "call" },
      { file: "tests/checkout.spec.ts", line: 2, text: "checkout();", kind: "call" },
      { file: "tests/excluded.spec.ts", line: 2, text: "checkout();", kind: "call" },
    ];

    const related = findRelatedTests(repo, repoIndex, "src/cart.ts", references);

    assert.equal(isTestPath("src/cart.test.ts"), true);
    assert.equal(isTestPath("src/contest.ts"), false);
    assert.equal(related[0], "src/cart.test.ts");
    assert.ok(related.includes("tests/checkout.spec.ts"));
    assert.equal(related.includes("tests/excluded.spec.ts"), false);
    assert.equal(related.includes("e2e/cart.spec.ts"), false);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("symbol collection captures imports, exports, synthetic symbols, and deleted entries", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-symbol-collection-"));
  try {
    const filePath = path.join(repo, "src/routes.ts");
    writeFile(repo, "src/helper.ts", "export function helper(): number { return 1; }\n");
    writeFile(
      repo,
      "src/routes.ts",
      [
        "import { helper } from './helper.js';",
        "",
        "export const handlers = Object.freeze({",
        "  read: () => helper(),",
        "  save,",
        "});",
        "",
        "export const routes = [",
        "  { method: 'GET', template: '/cart' },",
        "  ['POST', '/cart'],",
        "];",
        "",
        "export enum Status {",
        "  Active = 'active',",
        "}",
        "",
        "function save(): number { return helper(); }",
        "module.exports.default = handlers;",
      ].join("\n"),
    );

    const program = ts.createProgram({
      rootNames: [filePath],
      options: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.NodeNext,
        moduleResolution: ts.ModuleResolutionKind.NodeNext,
        strict: true,
      },
    });
    const source = program.getSourceFile(filePath);
    assert.ok(source);

    const symbols = collectSymbols(source, program.getTypeChecker());
    const preferredSymbols = preferSyntheticChildSymbols(symbols);
    const names = new Set(symbols.map((symbol) => symbol.analysis.name));
    const preferredNames = new Set(preferredSymbols.map((symbol) => symbol.analysis.name));
    const deletedLine = source.getLineAndCharacterOfPosition(source.text.indexOf("read:")).line + 1;
    const deletedSymbols = collectDeletedSymbols(source, symbols, [
      { line: deletedLine, text: "  remove: () => helper()," },
    ]);

    assert.deepEqual(collectImports(source), ["import { helper } from './helper.js';"]);
    assert.ok(collectExports(source).some((line) => line.includes("export const handlers")));
    assert.ok(collectExports(source).some((line) => line.includes("module.exports.default")));
    assert.ok(names.has("handlers"));
    assert.ok(names.has("read"));
    assert.ok(names.has("routes:GET /cart"));
    assert.ok(names.has("routes:POST /cart"));
    assert.ok(names.has("Status"));
    assert.ok(names.has("Active"));
    assert.equal(preferredNames.has("handlers"), false);
    assert.equal(preferredNames.has("routes"), false);
    assert.equal(preferredNames.has("Status"), false);
    assert.equal(deletedSymbols[0]?.analysis.name, "remove");
    assert.match(deletedSymbols[0]?.analysis.signature ?? "", /handlers removed entry remove/);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("reference merge prioritizes high signal refs and deduplicates", () => {
  const duplicateCall: Reference = {
    file: "src/cart.ts",
    line: 10,
    text: "calculateTotal(items);",
    kind: "call",
  };
  const refs: Reference[] = [
    { file: "src/cart.test.ts", line: 20, text: "calculateTotal(items);", kind: "call" },
    { file: "src/cart.ts", line: 3, text: "import { calculateTotal } from './cart.js';", kind: "import" },
    duplicateCall,
    { ...duplicateCall },
    { file: "src/cart.ts", line: 12, text: "total = calculateTotal(items);", kind: "write" },
  ];

  assert.deepEqual(mergeReferences(refs, 3), [
    duplicateCall,
    { file: "src/cart.ts", line: 12, text: "total = calculateTotal(items);", kind: "write" },
    { file: "src/cart.test.ts", line: 20, text: "calculateTotal(items);", kind: "call" },
  ]);

  const seen = new Set<string>();
  const added: Reference[] = [];
  addReference(added, seen, duplicateCall, 1);
  addReference(added, seen, { ...duplicateCall }, 1);
  addReference(added, seen, { file: "src/cart.ts", line: 11, text: "other();", kind: "call" }, 1);
  assert.deepEqual(added, [duplicateCall]);
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

test("repo index builder captures module, identifier, receiver, and cache metadata", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-"));
  const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-cache-"));
  try {
    writeFile(
      repo,
      "src/cart.ts",
      [
        "export class CartService {",
        "  total(price: number): number {",
        "    return price;",
        "  }",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "src/cart.test.ts",
      [
        "import { CartService } from './cart.js';",
        "",
        "const service = new CartService();",
        "service.total(1);",
      ].join("\n"),
    );

    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/cart.ts",
      "--index-cache-dir",
      cacheDir,
    ]);
    const first = buildRepoIndex(args);
    const second = buildRepoIndex(args);
    const testEntry = first.files.find((entry) => entry.relPath === "src/cart.test.ts");

    assert.ok(testEntry);
    assert.ok(testEntry.imports.some((entry) => entry.moduleSpecifier === "./cart.js"));
    assert.ok(testEntry.identifiers.some((entry) => entry.name === "CartService"));
    assert.ok(testEntry.receivers.some((entry) => entry.receiverName === "service" && entry.typeName === "CartService"));
    assert.equal(first.cacheStats?.misses, 2);
    assert.equal(first.cacheStats?.written, true);
    assert.equal(second.cacheStats?.hits, 2);
    assert.equal(second.cacheStats?.misses, 0);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("workspace references capture imports and filter unrelated member receivers", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-workspace-refs-"));
  try {
    const filePath = path.join(repo, "src/cart.ts");
    writeFile(repo, "package.json", JSON.stringify({ name: "workspace" }));
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
        "export class CartService {",
        "  total(): number {",
        "    return 1;",
        "  }",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "src/checkout.ts",
      [
        "import { CartService } from './cart.js';",
        "",
        "const service = new CartService();",
        "service.total();",
        "const other = { total: () => 0 };",
        "other.total();",
      ].join("\n"),
    );

    const program = ts.createProgram({
      rootNames: [filePath],
      options: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.NodeNext,
        moduleResolution: ts.ModuleResolutionKind.NodeNext,
        strict: true,
      },
    });
    const source = program.getSourceFile(filePath);
    assert.ok(source);

    const repoIndex = buildRepoIndex(parseArgs(["--repo", repo, "--changed", "src/cart.ts", "--no-index-cache"]));
    const symbols = collectSymbols(source, program.getTypeChecker());
    const classTarget = symbols.find((symbol) => symbol.analysis.name === "CartService");
    const methodTarget = symbols.find((symbol) => symbol.analysis.name === "total");
    assert.ok(classTarget);
    assert.ok(methodTarget);

    const importRefs = collectWorkspaceImportReferences(repo, repoIndex, classTarget, 20);
    assert.ok(importRefs.some((reference) => reference.file === "src/checkout.ts" && reference.kind === "import"));
    assert.ok(importRefs.some((reference) => reference.file === "src/checkout.ts" && reference.text.includes("new CartService()")));

    const memberRefs = collectWorkspaceMemberReferences(repo, repoIndex, methodTarget, 20);
    const unrelatedMemberRef: Reference = {
      file: "src/checkout.ts",
      line: 6,
      text: "other.total();",
      kind: "call",
    };
    const filteredMemberRefs = filterInvalidWorkspaceMemberReferences(
      repo,
      repoIndex,
      methodTarget,
      [...memberRefs, unrelatedMemberRef],
    );

    assert.ok(memberRefs.some((reference) => reference.text.includes("service.total()")));
    assert.equal(filteredMemberRefs.some((reference) => reference.text.includes("service.total()")), true);
    assert.equal(filteredMemberRefs.some((reference) => reference.text.includes("other.total()")), false);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("contract analysis captures declared types, schema receivers, and framework metadata", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-contract-analysis-"));
  try {
    const controllerPath = path.join(repo, "src/controller.ts");
    const contractsPath = path.join(repo, "src/contracts.ts");
    writeFile(
      repo,
      "tsconfig.json",
      JSON.stringify({
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
          experimentalDecorators: true,
        },
        include: ["src/**/*.ts"],
      }),
    );
    writeFile(
      repo,
      "src/contracts.ts",
      [
        "export interface CartItem {",
        "  price: number;",
        "}",
        "",
        "export const cartSchema = {",
        "  parse(value: unknown): CartItem {",
        "    return value as CartItem;",
        "  },",
        "};",
        "",
        "export function Controller(): ClassDecorator {",
        "  return () => {};",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "src/controller.ts",
      [
        "import { CartItem, Controller, cartSchema } from './contracts.js';",
        "",
        "@Controller()",
        "export class CartController {",
        "  checkout(item: CartItem): CartItem {",
        "    return cartSchema.parse(item);",
        "  }",
        "}",
      ].join("\n"),
    );

    const program = ts.createProgram({
      rootNames: [controllerPath, contractsPath],
      options: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.NodeNext,
        moduleResolution: ts.ModuleResolutionKind.NodeNext,
        strict: true,
        experimentalDecorators: true,
      },
    });
    const source = program.getSourceFile(controllerPath);
    assert.ok(source);
    const target = collectSymbols(source, program.getTypeChecker()).find((symbol) => symbol.analysis.name === "checkout");
    assert.ok(target);

    const contracts = collectSchemaContracts(program, program.getTypeChecker(), target, repo, 20);
    const metadata = collectFrameworkMetadata(target, repo, 20);

    assert.ok(
      contracts.some(
        (reference) =>
          reference.file === "src/contracts.ts" &&
          reference.kind === "contract" &&
          reference.text.includes("export interface CartItem"),
      ),
    );
    assert.ok(
      contracts.some(
        (reference) =>
          reference.file === "src/contracts.ts" &&
          reference.kind === "contract" &&
          reference.text.includes("export const cartSchema"),
      ),
    );
    assert.ok(metadata.some((reference) => reference.kind === "metadata" && reference.text.includes("@Controller()")));
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("reference analysis captures direct refs, implemented members, callees, and consumer impact", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-reference-analysis-"));
  try {
    const cartPath = path.join(repo, "src/cart.ts");
    const contractsPath = path.join(repo, "src/contracts.ts");
    const checkoutPath = path.join(repo, "src/checkout.ts");
    const routesPath = path.join(repo, "src/routes.ts");
    const appPath = path.join(repo, "src/app.ts");
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
    writeFile(repo, "src/contracts.ts", "export interface CartPort {\n  total(): number;\n}\n");
    writeFile(
      repo,
      "src/cart.ts",
      [
        "import type { CartPort } from './contracts.js';",
        "",
        "export function helper(): number {",
        "  return 1;",
        "}",
        "",
        "export class CartService implements CartPort {",
        "  total(): number {",
        "    return helper();",
        "  }",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "src/checkout.ts",
      [
        "import { CartService } from './cart.js';",
        "import type { CartPort } from './contracts.js';",
        "",
        "const service: CartPort = new CartService();",
        "",
        "export function checkout(): number {",
        "  return service.total();",
        "}",
      ].join("\n"),
    );
    writeFile(
      repo,
      "src/routes.ts",
      [
        "export const routes = [{ method: 'GET', template: '/cart' }];",
        "",
        "export function createRouter(): unknown {",
        "  return routes;",
        "}",
      ].join("\n"),
    );
    writeFile(repo, "src/app.ts", "import { createRouter } from './routes.js';\nexport function boot() {\n  return createRouter();\n}\n");

    const program = ts.createProgram({
      rootNames: [cartPath, contractsPath, checkoutPath, routesPath, appPath],
      options: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.NodeNext,
        moduleResolution: ts.ModuleResolutionKind.NodeNext,
        strict: true,
      },
    });
    const checker = program.getTypeChecker();
    const cartSource = program.getSourceFile(cartPath);
    const routesSource = program.getSourceFile(routesPath);
    assert.ok(cartSource);
    assert.ok(routesSource);

    const cartSymbols = collectSymbols(cartSource, checker);
    const classTarget = cartSymbols.find((symbol) => symbol.analysis.name === "CartService");
    const methodTarget = cartSymbols.find((symbol) => symbol.analysis.name === "total");
    assert.ok(classTarget);
    assert.ok(methodTarget);

    const classRefs = collectReferences(program, checker, classTarget, repo, 20);
    const implementedMemberRefs = collectImplementedMemberUsageReferences(program, checker, methodTarget, repo, 20);
    const callees = collectCallees(checker, methodTarget, repo, 20);

    assert.ok(classRefs.some((reference) => reference.text.includes("new CartService()")));
    assert.ok(implementedMemberRefs.some((reference) => reference.text.includes("service.total()")));
    assert.ok(callees.some((reference) => reference.file === "src/cart.ts" && reference.text.includes("helper(): number")));

    const routeTarget = collectSymbols(routesSource, checker).find((symbol) => symbol.analysis.name === "routes:GET /cart");
    assert.ok(routeTarget);
    const consumerImpact = collectReferenceConsumerImpact(program, checker, routeTarget, repo, 20);
    assert.ok(consumerImpact.references.some((reference) => reference.file === "src/app.ts" && reference.text.includes("createRouter()")));
    assert.ok(consumerImpact.callees.some((reference) => reference.file === "src/routes.ts" && reference.text.includes("createRouter")));
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
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
