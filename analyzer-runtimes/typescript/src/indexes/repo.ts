import fs from "node:fs";
import path from "node:path";

import { readRepoIndexCache, repoIndexCachePath, writeRepoIndexCache } from "./repo-cache.js";
import { orderRepoIndexPaths } from "./relevance.js";
import { indexSourceFile, isAnalyzableSourceFile } from "./source-file.js";
import {
  ANALYZER_SOURCE_BYTE_LIMIT,
  ANALYZER_SOURCE_FILE_LIMIT,
  REPO_INDEX_SEMANTIC_ENTRY_LIMIT,
} from "../constants.js";
import type { Args, RepoFileIndexEntry, RepoIndex } from "../types.js";
import {
  semanticEntryCountForFile,
  type IndexCollectionControl,
} from "./collection.js";
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
  const deadlineExhaustedBeforeWork = shouldStop();
  const cachePath = args.indexCacheEnabled ? repoIndexCachePath(repo, args.indexCacheDir) : null;
  const cache =
    cachePath &&
    !args.refreshIndexCache &&
    !deadlineExhaustedBeforeWork
      ? readRepoIndexCache(
          cachePath,
          inventory.fingerprint,
          {
            // Fingerprints cover inventory membership, not source contents.
            // Reuse entries across membership changes only because the loop
            // below filters to current paths and revalidates stable identity.
            allowInventoryMismatch: true,
            shouldStop,
          },
        )
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
  let workspacePartial = false;
  let unavailableFileCount = 0;
  const acceptedFileBytesByKey = new Map<string, number>();
  let acceptedBytes = 0;
  let sourceBudgetWarningEmitted = false;
  let semanticEntryCount = 0;
  let semanticEntryLimitReached = false;
  let collectionStopped = false;
  let collectionStopWarningEmitted = false;
  let semanticBudgetWarningEmitted = false;
  let expansionBudgetWarningEmitted = false;
  const markCollectionStopped = (): void => {
    partial = true;
    workspacePartial = true;
    if (collectionStopWarningEmitted) return;
    collectionStopWarningEmitted = true;
    warnings.push(
      "TypeScript repo index stopped because the analysis time budget was exhausted; workspace references are partial.",
    );
  };
  const markSemanticEntryLimitReached = (): void => {
    partial = true;
    workspacePartial = true;
    if (semanticBudgetWarningEmitted) return;
    semanticBudgetWarningEmitted = true;
    warnings.push(
      `TypeScript repo index semantic entry safety limit of ${REPO_INDEX_SEMANTIC_ENTRY_LIMIT} was reached; workspace references are partial.`,
    );
  };
  const markModuleTargetExpansionLimitReached = (): void => {
    partial = true;
    workspacePartial = true;
    if (expansionBudgetWarningEmitted) return;
    expansionBudgetWarningEmitted = true;
    warnings.push(
      "TypeScript module target expansion safety limit was reached; workspace references are partial.",
    );
  };
  const collectionControl: IndexCollectionControl = {
    shouldStop: () => {
      if (semanticEntryLimitReached || collectionStopped) return true;
      if (!shouldStop()) return false;
      collectionStopped = true;
      return true;
    },
    reserveEntry: () => collectionControl.reserveEntries(1),
    reserveEntries: (count) => {
      if (count < 0 || !Number.isSafeInteger(count)) {
        semanticEntryLimitReached = true;
        return false;
      }
      if (collectionControl.shouldStop()) return false;
      if (
        count >
        REPO_INDEX_SEMANTIC_ENTRY_LIMIT - semanticEntryCount
      ) {
        semanticEntryLimitReached = true;
        return false;
      }
      semanticEntryCount += count;
      return true;
    },
    markPartial: markSemanticEntryLimitReached,
  };
  const markUnavailable = (relPath: string, reason: string): void => {
    partial = true;
    workspacePartial = true;
    unavailableFileCount += 1;
    if (unavailableFileCount <= 10) {
      warnings.push(
        `TypeScript repo index source ${relPath} ${reason}; workspace references are partial.`,
      );
    }
  };
  const reserveSourceFile = (absPath: string, fileBytes: number): boolean => {
    const fileKey = canonicalPathKey(absPath);
    const previouslyAcceptedBytes = acceptedFileBytesByKey.get(fileKey);
    if (
      previouslyAcceptedBytes !== undefined &&
      fileBytes <= previouslyAcceptedBytes
    ) {
      return true;
    }
    const additionalBytes = fileBytes - (previouslyAcceptedBytes ?? 0);
    if (
      fileBytes > ANALYZER_SOURCE_BYTE_LIMIT ||
      (previouslyAcceptedBytes === undefined &&
        acceptedFileBytesByKey.size >= ANALYZER_SOURCE_FILE_LIMIT) ||
      additionalBytes > ANALYZER_SOURCE_BYTE_LIMIT - acceptedBytes
    ) {
      partial = true;
      workspacePartial = true;
      if (!sourceBudgetWarningEmitted) {
        sourceBudgetWarningEmitted = true;
        warnings.push(
          `TypeScript repo index source budget reached the safety limit of ${ANALYZER_SOURCE_FILE_LIMIT} repository files or ${ANALYZER_SOURCE_BYTE_LIMIT} bytes per file and in aggregate; workspace references are partial.`,
        );
      }
      return false;
    }
    acceptedFileBytesByKey.set(fileKey, fileBytes);
    acceptedBytes += additionalBytes;
    return true;
  };

  const orderedPaths =
    inventory.absPaths.length > ANALYZER_SOURCE_FILE_LIMIT
      ? orderRepoIndexPaths(
          args,
          inventory,
          cachedFiles,
          shouldStop,
          markModuleTargetExpansionLimitReached,
        )
      : inventory.absPaths;
  let startedIndexing = false;
  for (const absPath of orderedPaths) {
    if (
      deadlineExhaustedBeforeWork ||
      (startedIndexing && shouldStop())
    ) {
      collectionStopped = true;
      markCollectionStopped();
      break;
    }
    if (!isAnalyzableSourceFile(absPath)) continue;
    startedIndexing = true;
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
        if (!reserveSourceFile(absPath, inspection.identity.size)) {
          continue;
        }
        fileIdentities.set(canonicalPathKey(absPath), inspection.identity);
        if (
          !collectionControl.reserveEntries(
            semanticEntryCountForFile(cached),
          )
        ) {
          if (collectionStopped) markCollectionStopped();
          if (semanticEntryLimitReached) markSemanticEntryLimitReached();
          break;
        }
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
    let sourceBudgetRejected = false;
    const snapshot = readStableFile(
      absPath,
      isPermitted,
      (identity) => {
        const accepted = reserveSourceFile(absPath, identity.size);
        sourceBudgetRejected = !accepted;
        return accepted;
      },
    );
    if (!snapshot) {
      markUnavailable(relPath, "could not be read safely");
      continue;
    }
    if (snapshot.text === null) {
      if (!sourceBudgetRejected) {
        markUnavailable(relPath, "could not be read");
      }
      continue;
    }
    fileIdentities.set(canonicalPathKey(absPath), snapshot.identity);
    misses += 1;

    try {
      files.push(
        indexSourceFile(
          {
            repo,
            absPath,
            relPath,
            dev: snapshot.identity.dev,
            ino: snapshot.identity.ino,
            size: snapshot.identity.size,
            mtimeMs: snapshot.identity.mtimeMs,
            ctimeMs: snapshot.identity.ctimeMs,
            text: snapshot.text,
          },
          collectionControl,
        ),
      );
      if (collectionStopped) {
        markCollectionStopped();
        break;
      }
      if (semanticEntryLimitReached) {
        markSemanticEntryLimitReached();
        break;
      }
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
      ? writeRepoIndexCache(
          cachePath,
          files,
          inventory.fingerprint,
          shouldStop,
        )
      : { written: false, error: null };
  if (cacheWrite.error && cachePath) {
    warnings.push(`TypeScript repo index cache could not be written at ${cachePath}: ${cacheWrite.error}`);
  }
  if (cacheWrite.limited && cachePath) {
    warnings.push(
      `TypeScript repo index cache output exceeded its safety limits and was not written at ${cachePath}.`,
    );
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

  const repoIndex: RepoIndex = {
    files,
    packageByFile: new Map(),
    fileIdentities,
    cacheStats,
    partial,
    workspacePartial,
  };
  let lateExpansionWarningEmitted = false;
  let lateCacheSuppressionAttempted = false;
  repoIndex.markModuleResolutionPartial = (): void => {
    repoIndex.partial = true;
    repoIndex.workspacePartial = true;
    if (!lateExpansionWarningEmitted) {
      lateExpansionWarningEmitted = true;
      warnings.push(
        "TypeScript module target expansion safety limit was reached; workspace references and related tests are partial.",
      );
    }
    if (lateCacheSuppressionAttempted) return;
    lateCacheSuppressionAttempted = true;
    if (!repoIndex.cacheStats) return;
    try {
      fs.rmSync(repoIndex.cacheStats.path, { force: true });
      repoIndex.cacheStats.written = false;
    } catch (error) {
      const reason =
        error instanceof Error ? error.message : String(error);
      warnings.push(
        `TypeScript repo index cache could not be removed after partial module resolution at ${repoIndex.cacheStats.path}: ${reason}`,
      );
    }
  };
  return repoIndex;
}
