import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { parseArgs } from "../dist/cli.js";
import { buildRepoIndex } from "../dist/repo-index.js";
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
