import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  ANALYZER_SOURCE_FILE_LIMIT,
  REPO_INDEX_CACHE_BYTE_LIMIT,
  REPO_INDEX_CACHE_VERSION,
  REPO_INDEX_SEMANTIC_ENTRY_LIMIT,
} from "../dist/constants.js";
import { indexSourceFile } from "../dist/indexes/source-file.js";
import { readRepoIndexCache, repoIndexCachePath, writeRepoIndexCache } from "../dist/indexes/repo-cache.js";
import type { RepoFileIndexEntry } from "../dist/types.js";

function emptyRepoFile(
  repo: string,
  relPath = "src/cart.ts",
): RepoFileIndexEntry {
  return {
    absPath: path.join(repo, relPath),
    relPath,
    relLower: relPath.toLowerCase(),
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
}

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

test("repo index cache rejects the previous format before JSON parsing", () => {
  const cacheHome = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-cache-old-version-"),
  );
  const cachePath = path.join(cacheHome, "index.json");
  const originalJsonParse = JSON.parse;
  try {
    assert.ok(REPO_INDEX_CACHE_VERSION > 21);
    fs.writeFileSync(
      cachePath,
      JSON.stringify({
        version: 21,
        inventoryFingerprint: null,
        files: [],
      }),
      "utf8",
    );
    let parseCalls = 0;
    JSON.parse = ((text: string, reviver?: Parameters<typeof JSON.parse>[1]) => {
      parseCalls += 1;
      return originalJsonParse(text, reviver);
    }) as typeof JSON.parse;

    assert.equal(readRepoIndexCache(cachePath), null);
    assert.equal(parseCalls, 0);
  } finally {
    JSON.parse = originalJsonParse;
    fs.rmSync(cacheHome, { recursive: true, force: true });
  }
});

test("repo index cache rejects oversized files before reading their contents", () => {
  const cacheHome = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-cache-byte-limit-"),
  );
  const cachePath = path.join(cacheHome, "index.json");
  const originalReadFileSync = fs.readFileSync;
  try {
    fs.writeFileSync(cachePath, "");
    fs.truncateSync(cachePath, REPO_INDEX_CACHE_BYTE_LIMIT + 1);
    let contentReadAttempted = false;
    fs.readFileSync = ((
      candidate: fs.PathOrFileDescriptor,
      ...rest: unknown[]
    ) => {
      if (
        candidate === cachePath ||
        (typeof candidate === "number" &&
          fs.fstatSync(candidate).size > REPO_INDEX_CACHE_BYTE_LIMIT)
      ) {
        contentReadAttempted = true;
      }
      return (originalReadFileSync as (...args: unknown[]) => string | Buffer)(
        candidate,
        ...rest,
      );
    }) as typeof fs.readFileSync;

    assert.equal(readRepoIndexCache(cachePath), null);
    assert.equal(contentReadAttempted, false);
  } finally {
    fs.readFileSync = originalReadFileSync;
    fs.rmSync(cacheHome, { recursive: true, force: true });
  }
});

test("repo index cache rejects excessive file entries before JSON parsing", () => {
  const cacheHome = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-cache-entry-limit-"),
  );
  const cachePath = path.join(cacheHome, "index.json");
  const originalJsonParse = JSON.parse;
  try {
    const file = emptyRepoFile(cacheHome);
    fs.writeFileSync(
      cachePath,
      JSON.stringify({
        version: REPO_INDEX_CACHE_VERSION,
        inventoryFingerprint: null,
        files: Array.from(
          { length: ANALYZER_SOURCE_FILE_LIMIT + 1 },
          (_, index) => ({
            ...file,
            relPath: `src/file-${index}.ts`,
          }),
        ),
      }),
      "utf8",
    );
    let parseCalls = 0;
    JSON.parse = ((text: string, reviver?: Parameters<typeof JSON.parse>[1]) => {
      parseCalls += 1;
      return originalJsonParse(text, reviver);
    }) as typeof JSON.parse;

    assert.equal(readRepoIndexCache(cachePath), null);
    assert.equal(parseCalls, 0);
  } finally {
    JSON.parse = originalJsonParse;
    fs.rmSync(cacheHome, { recursive: true, force: true });
  }
});

test("repo index cache suppresses serialized output beyond its byte limit", () => {
  const cacheHome = fs.mkdtempSync(
    path.join(os.tmpdir(), "apex-ray-ts-cache-output-limit-"),
  );
  const cachePath = path.join(cacheHome, "index.json");
  try {
    const file = emptyRepoFile(cacheHome);
    const repeatedName = "identifier".repeat(32);
    const reference = {
      file: "src/cart.ts",
      line: 1,
      kind: "read" as const,
    };
    file.identifiers = Array.from(
      { length: REPO_INDEX_SEMANTIC_ENTRY_LIMIT },
      () => ({
        name: repeatedName,
        namespaceQualifier: null,
        reference,
      }),
    );

    const result = writeRepoIndexCache(cachePath, [file], null);

    assert.equal(result.written, false);
    assert.equal(result.error, null);
    assert.equal(result.limited, true);
    assert.equal(fs.existsSync(cachePath), false);
  } finally {
    fs.rmSync(cacheHome, { recursive: true, force: true });
  }
});
