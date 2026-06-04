import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { readRepoIndexCache, repoIndexCachePath, writeRepoIndexCache } from "../dist/indexes/repo-cache.js";
import type { RepoFileIndexEntry } from "../dist/types.js";

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
