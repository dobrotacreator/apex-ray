import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { parseArgs } from "../dist/cli.js";

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
