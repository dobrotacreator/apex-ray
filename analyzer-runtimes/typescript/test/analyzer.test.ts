import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { analyze } from "../dist/analyzer.js";
import { parseArgs } from "../dist/cli.js";
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
