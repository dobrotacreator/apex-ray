import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { parseArgs } from "../dist/cli.js";
import { buildRepoIndex } from "../dist/indexes/repo.js";
import { writeFile } from "./helpers.js";

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

test("repo index builder limits indexing to a file manifest", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-manifest-"));
  const cacheDir = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-manifest-cache-"));
  try {
    writeFile(repo, "src/cart.ts", "export const cart = true;\n");
    writeFile(repo, "src/consumer.ts", "export const consumer = true;\n");
    writeFile(repo, ".venv/vendor.min.js", "const ignored = true;\n");
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 1, files: ["src/cart.ts", "src/consumer.ts"] }),
      "utf8",
    );

    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/cart.ts",
      "--file-manifest",
      manifestPath,
      "--index-cache-dir",
      cacheDir,
    ]);
    const first = buildRepoIndex(args);
    const second = buildRepoIndex(args);

    assert.deepEqual(
      first.files.map((entry) => entry.relPath),
      ["src/cart.ts", "src/consumer.ts"],
    );
    assert.equal(first.cacheStats?.misses, 2);
    assert.equal(first.cacheStats?.written, true);
    assert.equal(second.cacheStats?.hits, 2);
    assert.equal(second.cacheStats?.misses, 0);

    fs.writeFileSync(manifestPath, JSON.stringify({ version: 1, files: ["src/cart.ts"] }), "utf8");
    const narrowed = buildRepoIndex(args);
    assert.deepEqual(narrowed.files.map((entry) => entry.relPath), ["src/cart.ts"]);
    assert.equal(narrowed.cacheStats?.hits, 1);
    assert.equal(narrowed.cacheStats?.misses, 0);
    assert.equal(narrowed.cacheStats?.written, true);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheDir, { recursive: true, force: true });
  }
});

test("repo index builder rejects manifest paths outside the repository", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-manifest-boundary-"));
  try {
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(manifestPath, JSON.stringify({ version: 1, files: ["../outside.ts"] }), "utf8");
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/cart.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    assert.throws(() => buildRepoIndex(args), /outside the repository/);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
  }
});

test("repo inventory rejects manifest symlinks that resolve outside the repository", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-manifest-symlink-"));
  const outsideRoot = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-index-manifest-outside-"));
  try {
    writeFile(outsideRoot, "outside.d.ts", "declare const outsideSecret: string;\n");
    fs.mkdirSync(path.join(repo, "src"), { recursive: true });
    fs.symlinkSync(path.join(outsideRoot, "outside.d.ts"), path.join(repo, "src", "outside.d.ts"));
    const manifestPath = path.join(repo, "typescript-files.json");
    fs.writeFileSync(
      manifestPath,
      JSON.stringify({ version: 1, files: ["src/outside.d.ts"] }),
      "utf8",
    );
    const args = parseArgs([
      "--repo",
      repo,
      "--changed",
      "src/cart.ts",
      "--file-manifest",
      manifestPath,
      "--no-index-cache",
    ]);

    assert.throws(() => buildRepoIndex(args), /outside the repository/);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(outsideRoot, { recursive: true, force: true });
  }
});
