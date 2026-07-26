import path from "node:path";

import { readRepoIndexCache, repoIndexCachePath, writeRepoIndexCache } from "./repo-cache.js";
import { indexSourceFile, isAnalyzableSourceFile } from "./source-file.js";
import type { Args, RepoFileIndexEntry, RepoIndex } from "../types.js";
import {
  canonicalPathKey,
  inspectStableFile,
  normalizeRelPath,
  readStableFile,
  sameStableFileIdentity,
} from "../utils.js";
import { loadRepoFileInventory, type RepoFileInventory } from "../workspace/inventory.js";

export { commonJsExportEntries } from "./import-export.js";

export function buildRepoIndex(
  args: Args,
  warnings: string[] = [],
  inventory: RepoFileInventory = loadRepoFileInventory(args),
  shouldStop: () => boolean = () => false,
): RepoIndex {
  const repo = args.repo;
  const files: RepoFileIndexEntry[] = [];
  const fileIdentities = new Map<
    string,
    import("../utils.js").StableFileIdentity
  >();
  const cachePath = args.indexCacheEnabled ? repoIndexCachePath(repo, args.indexCacheDir) : null;
  const cache =
    cachePath && !args.refreshIndexCache
      ? readRepoIndexCache(cachePath, inventory.fingerprint)
      : null;
  const cachedFiles = new Map(
    (cache?.files ?? []).map((entry) => [
      canonicalPathKey(path.resolve(repo, entry.relPath)),
      entry,
    ]),
  );
  let hits = 0;
  let misses = 0;
  let partial = inventory.partial;
  let unavailableFileCount = 0;
  const markUnavailable = (relPath: string, reason: string): void => {
    partial = true;
    unavailableFileCount += 1;
    if (unavailableFileCount <= 10) {
      warnings.push(
        `TypeScript repo index source ${relPath} ${reason}; workspace references are partial.`,
      );
    }
  };

  for (const absPath of inventory.absPaths) {
    if (shouldStop()) {
      if (!partial) {
        warnings.push(
          "TypeScript repo index stopped because the analysis time budget was exhausted; workspace references are partial.",
        );
      }
      partial = true;
      break;
    }
    if (!isAnalyzableSourceFile(absPath)) continue;
    const relPath = normalizeRelPath(path.relative(repo, absPath));
    const cached = cachedFiles.get(canonicalPathKey(absPath));
    const isPermitted = (resolvedPath: string, realPath: string): boolean =>
      inventory.pathKeys.has(canonicalPathKey(resolvedPath)) &&
      inventory.pathKeys.has(canonicalPathKey(realPath));
    if (cached) {
      const inspection = inspectStableFile(absPath, isPermitted);
      if (
        inspection &&
        sameStableFileIdentity(cached, inspection.identity)
      ) {
        fileIdentities.set(canonicalPathKey(absPath), inspection.identity);
        hits += 1;
        files.push({
          absPath: path.resolve(absPath),
          relPath,
          relLower: relPath.toLowerCase(),
          dev: inspection.identity.dev,
          ino: inspection.identity.ino,
          size: cached.size,
          mtimeMs: cached.mtimeMs,
          ctimeMs: inspection.identity.ctimeMs,
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
    }
    const snapshot = readStableFile(
      absPath,
      isPermitted,
    );
    if (!snapshot) {
      markUnavailable(relPath, "could not be read safely");
      continue;
    }
    fileIdentities.set(canonicalPathKey(absPath), snapshot.identity);
    misses += 1;
    if (snapshot.text === null) {
      markUnavailable(relPath, "could not be read");
      continue;
    }

    try {
      files.push(indexSourceFile({
        repo,
        absPath,
        relPath,
        dev: snapshot.identity.dev,
        ino: snapshot.identity.ino,
        size: snapshot.identity.size,
        mtimeMs: snapshot.identity.mtimeMs,
        ctimeMs: snapshot.identity.ctimeMs,
        text: snapshot.text,
      }));
    } catch {
      markUnavailable(relPath, "could not be indexed safely");
    }
  }
  if (unavailableFileCount > 10) {
    warnings.push(
      `${unavailableFileCount - 10} additional TypeScript repo index source files were unavailable; workspace references are partial.`,
    );
  }

  const shouldWriteCache =
    !partial &&
    cachePath !== null &&
    (cache === null || args.refreshIndexCache || misses > 0 || cachedFiles.size !== files.length);
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
    fileIdentities,
    cacheStats,
    partial,
  };
}
