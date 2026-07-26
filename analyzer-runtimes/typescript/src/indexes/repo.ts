import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

import { readRepoIndexCache, repoIndexCachePath, writeRepoIndexCache } from "./repo-cache.js";
import { indexSourceFile, isAnalyzableSourceFile } from "./source-file.js";
import type { Args, RepoFileIndexEntry, RepoIndex } from "../types.js";
import { isInsideRepo, isRecord, normalizeRelPath, readUtf8, walk } from "../utils.js";

export { commonJsExportEntries } from "./import-export.js";

interface RepoFileInventory {
  absPaths: string[];
  fingerprint: string | null;
}

export function buildRepoIndex(args: Args, warnings: string[] = []): RepoIndex {
  const repo = args.repo;
  const files: RepoFileIndexEntry[] = [];
  const inventory = repoFileInventory(args);
  const cachePath = args.indexCacheEnabled ? repoIndexCachePath(repo, args.indexCacheDir) : null;
  const cache =
    cachePath && !args.refreshIndexCache
      ? readRepoIndexCache(cachePath, inventory.fingerprint)
      : null;
  const cachedFiles = new Map((cache?.files ?? []).map((entry) => [entry.relPath, entry]));
  let hits = 0;
  let misses = 0;

  for (const absPath of inventory.absPaths) {
    if (!isAnalyzableSourceFile(absPath)) continue;
    let stat: fs.Stats;
    try {
      stat = fs.lstatSync(absPath);
    } catch {
      continue;
    }
    if (!stat.isFile()) continue;
    const relPath = normalizeRelPath(path.relative(repo, absPath));
    const cached = cachedFiles.get(relPath);
    if (cached && cached.size === stat.size && cached.mtimeMs === stat.mtimeMs) {
      hits += 1;
      files.push({
        absPath: path.resolve(absPath),
        relPath,
        relLower: relPath.toLowerCase(),
        size: cached.size,
        mtimeMs: cached.mtimeMs,
        imports: cached.imports,
        exports: cached.exports,
        identifiers: cached.identifiers,
        receivers: cached.receivers,
        typeAliases: cached.typeAliases,
        classHeritages: cached.classHeritages,
        diProviders: cached.diProviders,
        diInjections: cached.diInjections,
      });
      continue;
    }

    misses += 1;
    const text = readUtf8(absPath);
    if (text === null) continue;

    files.push(
      indexSourceFile({
        repo,
        absPath,
        relPath,
        size: stat.size,
        mtimeMs: stat.mtimeMs,
        text,
      }),
    );
  }

  const shouldWriteCache =
    cachePath !== null && (cache === null || args.refreshIndexCache || misses > 0 || cachedFiles.size !== files.length);
  const cacheWrite =
    shouldWriteCache && cachePath
      ? writeRepoIndexCache(cachePath, files, inventory.fingerprint)
      : { written: false, error: null };
  if (cacheWrite.error && cachePath) {
    warnings.push(`TypeScript repo index cache could not be written at ${cachePath}: ${cacheWrite.error}`);
  }
  const cacheStats = cachePath
    ? {
        path: cachePath,
        files: files.length,
        hits,
        misses,
        written: cacheWrite.written,
      }
    : null;

  return {
    files,
    packageByFile: new Map(),
    cacheStats,
  };
}

function repoFileInventory(args: Args): RepoFileInventory {
  if (!args.fileManifestPath) {
    const absPaths: string[] = [];
    walk(args.repo, (absPath) => absPaths.push(absPath));
    return { absPaths, fingerprint: null };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(fs.readFileSync(args.fileManifestPath, "utf8"));
  } catch (error) {
    throw new Error(
      `Could not read TypeScript file manifest ${args.fileManifestPath}: ${
        error instanceof Error ? error.message : String(error)
      }`,
    );
  }
  if (
    !isRecord(parsed) ||
    parsed.version !== 1 ||
    !Array.isArray(parsed.files) ||
    !parsed.files.every((value) => typeof value === "string")
  ) {
    throw new Error(`Invalid TypeScript file manifest: ${args.fileManifestPath}`);
  }

  const relPaths = [...new Set(parsed.files.map((value) => normalizeRelPath(value)))].sort();
  const absPaths = relPaths.map((relPath) => {
    const absPath = path.resolve(args.repo, relPath);
    if (path.isAbsolute(relPath) || !isInsideRepo(args.repo, absPath)) {
      throw new Error(`TypeScript file manifest path is outside the repository: ${relPath}`);
    }
    return absPath;
  });
  const fingerprint = crypto.createHash("sha256").update(relPaths.join("\0")).digest("hex");
  return { absPaths, fingerprint };
}
