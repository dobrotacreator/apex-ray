import fs from "node:fs";
import path from "node:path";

import { readRepoIndexCache, repoIndexCachePath, writeRepoIndexCache } from "./repo-cache.js";
import { indexSourceFile, isAnalyzableSourceFile } from "./source-file.js";
import type { Args, RepoFileIndexEntry, RepoIndex } from "../types.js";
import { normalizeRelPath, readUtf8, walk } from "../utils.js";

export { commonJsExportEntries } from "./import-export.js";

export function buildRepoIndex(args: Args): RepoIndex {
  const repo = args.repo;
  const files: RepoFileIndexEntry[] = [];
  const cachePath = args.indexCacheEnabled ? repoIndexCachePath(repo, args.indexCacheDir) : null;
  const cache = cachePath && !args.refreshIndexCache ? readRepoIndexCache(cachePath) : null;
  const cachedFiles = new Map((cache?.files ?? []).map((entry) => [entry.relPath, entry]));
  let hits = 0;
  let misses = 0;

  walk(repo, (absPath) => {
    if (!isAnalyzableSourceFile(absPath)) return;
    const stat = fs.statSync(absPath);
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
      return;
    }

    misses += 1;
    const text = readUtf8(absPath);
    if (text === null) return;

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
  });

  const shouldWriteCache =
    cachePath !== null && (cache === null || args.refreshIndexCache || misses > 0 || cachedFiles.size !== files.length);
  const cacheStats = cachePath
    ? {
        path: cachePath,
        files: files.length,
        hits,
        misses,
        written: shouldWriteCache ? writeRepoIndexCache(cachePath, files) : false,
      }
    : null;

  return {
    files,
    packageByFile: new Map(),
    cacheStats,
  };
}
