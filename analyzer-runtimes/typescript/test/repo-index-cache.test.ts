import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { indexSourceFile } from "../dist/indexes/source-file.js";
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
      dev: 1,
      ino: 2,
      size: 10,
      mtimeMs: 123,
      ctimeMs: 124,
      imports: [],
      exports: [],
      identifiers: [],
      receivers: [],
      typeAliases: [],
      classHeritages: [],
      diProviders: [],
      diInjections: [],
    };

    assert.deepEqual(writeRepoIndexCache(cachePath, [file], null), { written: true, error: null });
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

test("repo index cache stores identifier coordinates without duplicated source text", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-compact-cache-repo-"));
  const cacheHome = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-compact-cache-home-"));
  try {
    const text = `export const total = ${Array.from({ length: 1_000 }, () => "cart.total").join(" + ")};\n`;
    const absPath = path.join(repo, "src/cart.ts");
    const entry = indexSourceFile({
      repo,
      absPath,
      relPath: "src/cart.ts",
      dev: 1,
      ino: 2,
      size: Buffer.byteLength(text),
      mtimeMs: 123,
      ctimeMs: 124,
      text,
    });
    const cachePath = path.join(cacheHome, "index.json");

    assert.equal(writeRepoIndexCache(cachePath, [entry], null).written, true);
    const raw = JSON.parse(fs.readFileSync(cachePath, "utf8")) as {
      files: Array<{ identifiers: Array<{ reference: Record<string, unknown> }> }>;
    };

    assert.ok(raw.files[0].identifiers.length > 0);
    assert.ok(raw.files[0].identifiers.every((identifier) => !("text" in identifier.reference)));
    assert.ok(fs.statSync(cachePath).size < Buffer.byteLength(text) * 30);
    assert.ok(readRepoIndexCache(cachePath));
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheHome, { recursive: true, force: true });
  }
});

test("repo index cache reports write failures and removes its temporary file", () => {
  const repo = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-cache-write-failure-repo-"));
  const cacheHome = fs.mkdtempSync(path.join(os.tmpdir(), "apex-ray-ts-cache-write-failure-home-"));
  try {
    const cachePath = path.join(cacheHome, "index.json");
    fs.mkdirSync(cachePath);
    const file: RepoFileIndexEntry = {
      absPath: path.join(repo, "src/cart.ts"),
      relPath: "src/cart.ts",
      relLower: "src/cart.ts",
      dev: 1,
      ino: 2,
      size: 10,
      mtimeMs: 123,
      ctimeMs: 124,
      imports: [],
      exports: [],
      identifiers: [],
      receivers: [],
      typeAliases: [],
      classHeritages: [],
      diProviders: [],
      diInjections: [],
    };

    const result = writeRepoIndexCache(cachePath, [file], null);

    assert.equal(result.written, false);
    assert.ok(result.error);
    assert.deepEqual(fs.readdirSync(cacheHome), ["index.json"]);
  } finally {
    fs.rmSync(repo, { recursive: true, force: true });
    fs.rmSync(cacheHome, { recursive: true, force: true });
  }
});
