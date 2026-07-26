import path from "node:path";

import { collectFrameworkMetadata, collectSchemaContracts } from "./contracts/analysis.js";
import { REFERENCE_COLLECTION_LIMIT, REFERENCE_LIMIT } from "./constants.js";
import {
  collectCallees,
  collectImplementedMemberUsageReferences,
  collectReferenceConsumerImpact,
  collectReferences,
  ReferenceScanCancelled,
} from "./references/analysis.js";
import { mergeReferences } from "./references/merge.js";
import { buildRepoIndex } from "./indexes/repo.js";
import { createProgramContexts } from "./program.js";
import {
  collectDeletedSymbols,
  collectExports,
  collectImports,
  collectSymbols,
  preferSyntheticChildSymbols,
} from "./symbols/collection.js";
import { findRelatedTests, isTestPath } from "./test-discovery.js";
import type {
  AnalyzerResult,
  AnalyzerShardFailure,
  Args,
  CollectedSymbol,
  FileAnalysis,
  Reference,
} from "./types.js";
import { canonicalPathKey, rangesOverlap } from "./utils.js";
import {
  collectProviderTokenInjectionReferences,
  collectWorkspaceDiReferences,
  collectWorkspaceImportReferences,
  collectWorkspaceMemberReferences,
  filterInvalidWorkspaceMemberReferences,
} from "./workspace/references.js";
import { loadRepoFileInventory, type RepoFileInventory } from "./workspace/inventory.js";

export type {
  AnalyzerResult,
  AnalyzerSymbol,
  Args,
  DeletedLine,
  FileAnalysis,
  Reference,
  ReferenceKind,
  SymbolKind,
} from "./types.js";

export function analyze(args: Args): AnalyzerResult {
  const warnings: string[] = [];
  const budgetExhausted = analysisBudget(args.analysisTimeBudgetMs);
  const inventory = loadRepoFileInventory(args, { shouldStop: budgetExhausted });
  if (inventory.partialReason) warnings.push(inventory.partialReason);
  if (budgetExhausted()) {
    const reason = `TypeScript analyzer internal budget exhausted after ${args.analysisTimeBudgetMs ?? 0}ms`;
    warnings.push(
      `${reason}; skipped ${args.changed.length} changed file${args.changed.length === 1 ? "" : "s"}.`,
    );
    return {
      language: "typescript",
      projectRoot: args.repo,
      tsconfigPath: null,
      files: [],
      warnings,
      indexCache: null,
      partial: true,
      failedFiles: [...args.changed],
      shardFailures: [
        {
          index: 1,
          total: 1,
          files: [...args.changed],
          reason,
          status: "timeout",
        },
      ],
    };
  }
  const contextsByFile = createProgramContexts(
    args,
    warnings,
    inventory,
    budgetExhausted,
  );
  const repoIndex = buildRepoIndex(args, warnings, inventory, budgetExhausted);
  const changedPathKeys = new Set(
    args.changed.map((fileName) => canonicalPathKey(path.resolve(args.repo, fileName))),
  );
  const sourcePermissionCache = new WeakMap<import("typescript").SourceFile, boolean>();
  const sourceAllowed = (source: import("typescript").SourceFile): boolean => {
    const cached = sourcePermissionCache.get(source);
    if (cached !== undefined) return cached;
    const pathKey = canonicalPathKey(source.fileName);
    const allowed = inventory.pathKeys.has(pathKey) || changedPathKeys.has(pathKey);
    sourcePermissionCache.set(source, allowed);
    return allowed;
  };
  const syntheticReferenceScanCache = new Map<string, ReferenceScanResult>();
  const failedFileSet = new Set<string>();
  const failedFiles: string[] = [];
  const shardFailures: AnalyzerShardFailure[] = [];

  const markFileFailed = (file: string, reason: string): void => {
    if (failedFileSet.has(file)) return;
    failedFileSet.add(file);
    failedFiles.push(file);
    warnings.push(reason);
    shardFailures.push({
      index: 1,
      total: 1,
      files: [file],
      reason,
      status: "failed",
    });
  };

  const markBudgetExhausted = (filesToSkip: string[]): void => {
    const skippedFiles = filesToSkip.filter((file) => !failedFileSet.has(file));
    if (skippedFiles.length === 0) return;
    for (const file of skippedFiles) {
      failedFileSet.add(file);
      failedFiles.push(file);
    }
    const reason = `TypeScript analyzer internal budget exhausted after ${args.analysisTimeBudgetMs ?? 0}ms`;
    warnings.push(`${reason}; skipped ${skippedFiles.length} changed file${skippedFiles.length === 1 ? "" : "s"}.`);
    shardFailures.push({
      index: 1,
      total: 1,
      files: skippedFiles,
      reason,
      status: "timeout",
    });
  };

  const files: FileAnalysis[] = [];
  for (let changedIndex = 0; changedIndex < args.changed.length; changedIndex += 1) {
    const changedFile = args.changed[changedIndex];
    if (budgetExhausted()) {
      markBudgetExhausted(args.changed.slice(changedIndex));
      break;
    }

    const context = contextsByFile.get(changedFile);
    if (!context) {
      markFileFailed(
        changedFile,
        `No TypeScript program could be created for changed file: ${changedFile}`,
      );
      continue;
    }

    const { program, checker } = context;
    const absPath = path.resolve(args.repo, changedFile);
    const source = program.getSourceFile(absPath);
    if (!source) {
      markFileFailed(
        changedFile,
        `Changed file is not part of the TypeScript program: ${changedFile}`,
      );
      continue;
    }

    const collectedSymbols = collectSymbols(source, checker);
    const symbols = collectedSymbols.map((symbol) => symbol.analysis);
    const imports = collectImports(source);
    const exports = collectExports(source);
    const ranges = args.changedRanges.get(changedFile) ?? [];
    const deletedCollectedSymbols = collectDeletedSymbols(
      source,
      collectedSymbols,
      args.deletedLines.get(changedFile) ?? [],
    );
    const changedCollectedSymbols = preferSyntheticChildSymbols([
      ...deletedCollectedSymbols,
      ...collectedSymbols.filter((symbol) =>
        ranges.some(([start, end]) => rangesOverlap(symbol.analysis.startLine, symbol.analysis.endLine, start, end)),
      ),
    ]);
    const isChangedTestFile = isTestPath(changedFile.toLowerCase());
    let completedFile = true;
    if (budgetExhausted()) {
      markBudgetExhausted(args.changed.slice(changedIndex));
      break;
    }

    for (let symbolIndex = 0; symbolIndex < changedCollectedSymbols.length; symbolIndex += 1) {
      if (budgetExhausted()) {
        markBudgetExhausted(args.changed.slice(changedIndex));
        completedFile = false;
        break;
      }

      const symbol = changedCollectedSymbols[symbolIndex];
      if (isChangedTestFile) {
        symbol.analysis.references = [];
        symbol.analysis.callees = [];
        symbol.analysis.contracts = [];
        symbol.analysis.metadata = [];
        continue;
      }
      const referenceScanCacheKey = syntheticReferenceScanCacheKey(symbol);
      const cachedReferenceScan = referenceScanCacheKey ? syntheticReferenceScanCache.get(referenceScanCacheKey) : undefined;
      const referenceScan =
        cachedReferenceScan ??
        collectReferenceScan(
          program,
          checker,
          symbol,
          args.repo,
          symbol.analysis.name.includes(":"),
          budgetExhausted,
          sourceAllowed,
        );
      if (!referenceScan.completed || budgetExhausted()) {
        markBudgetExhausted(args.changed.slice(changedIndex));
        completedFile = false;
        break;
      }
      if (referenceScanCacheKey && !cachedReferenceScan) {
        syntheticReferenceScanCache.set(referenceScanCacheKey, referenceScan);
      }

      symbol.analysis.references = mergeReferences(
        [
          ...referenceScan.directReferences,
          ...referenceScan.consumerImpact.references,
          ...collectWorkspaceImportReferences(args.repo, repoIndex, symbol, REFERENCE_COLLECTION_LIMIT),
          ...collectWorkspaceMemberReferences(args.repo, repoIndex, symbol, REFERENCE_COLLECTION_LIMIT),
          ...collectWorkspaceDiReferences(args.repo, repoIndex, symbol, REFERENCE_COLLECTION_LIMIT),
          ...collectProviderTokenInjectionReferences(args.repo, repoIndex, symbol, REFERENCE_COLLECTION_LIMIT),
        ],
        REFERENCE_LIMIT,
      );
      symbol.analysis.references = filterInvalidWorkspaceMemberReferences(args.repo, repoIndex, symbol, symbol.analysis.references);
      symbol.analysis.references = filterReferencesByInventory(
        symbol.analysis.references,
        args.repo,
        inventory,
        changedPathKeys,
      );
      if (budgetExhausted()) {
        markBudgetExhausted(args.changed.slice(changedIndex));
        completedFile = false;
        break;
      }

      let calleeReferences: Reference[];
      try {
        calleeReferences = collectCallees(
          checker,
          symbol,
          args.repo,
          REFERENCE_COLLECTION_LIMIT,
          budgetExhausted,
          sourceAllowed,
        );
      } catch (error) {
        if (error instanceof ReferenceScanCancelled) {
          markBudgetExhausted(args.changed.slice(changedIndex));
          completedFile = false;
          break;
        }
        throw error;
      }
      symbol.analysis.callees = mergeReferences([...calleeReferences, ...referenceScan.consumerImpact.callees], REFERENCE_LIMIT);
      symbol.analysis.callees = filterReferencesByInventory(
        symbol.analysis.callees,
        args.repo,
        inventory,
        changedPathKeys,
      );
      if (budgetExhausted()) {
        markBudgetExhausted(args.changed.slice(changedIndex));
        completedFile = false;
        break;
      }

      symbol.analysis.contracts = mergeReferences(
        collectSchemaContracts(
          program,
          checker,
          symbol,
          args.repo,
          REFERENCE_COLLECTION_LIMIT,
          sourceAllowed,
        ),
        REFERENCE_LIMIT,
      );
      symbol.analysis.contracts = filterReferencesByInventory(
        symbol.analysis.contracts,
        args.repo,
        inventory,
        changedPathKeys,
      );
      if (budgetExhausted()) {
        markBudgetExhausted(args.changed.slice(changedIndex));
        completedFile = false;
        break;
      }

      symbol.analysis.metadata = mergeReferences(
        collectFrameworkMetadata(symbol, args.repo, REFERENCE_COLLECTION_LIMIT),
        REFERENCE_LIMIT,
      );
      symbol.analysis.metadata = filterReferencesByInventory(
        symbol.analysis.metadata,
        args.repo,
        inventory,
        changedPathKeys,
      );
      if (budgetExhausted()) {
        markBudgetExhausted(args.changed.slice(changedIndex));
        completedFile = false;
        break;
      }
    }
    if (!completedFile) {
      break;
    }

    const changedReferences = changedCollectedSymbols.flatMap((symbol) => symbol.analysis.references);
    if (budgetExhausted()) {
      markBudgetExhausted(args.changed.slice(changedIndex));
      break;
    }
    const relatedTests = findRelatedTests(args.repo, repoIndex, changedFile, changedReferences);
    if (budgetExhausted()) {
      markBudgetExhausted(args.changed.slice(changedIndex));
      break;
    }
    files.push({
      path: changedFile,
      tsconfigPath: context.tsconfigPath,
      symbols,
      imports,
      exports,
      relatedTests,
      changedSymbols: changedCollectedSymbols.map((symbol) => symbol.analysis),
    });
  }

  if (
    inventory.partialReason &&
    !warnings.includes(inventory.partialReason)
  ) {
    warnings.push(inventory.partialReason);
  }
  const tsconfigPaths = new Set(files.map((file) => file.tsconfigPath).filter((value): value is string => Boolean(value)));
  return {
    language: "typescript",
    projectRoot: args.repo,
    tsconfigPath: tsconfigPaths.size === 1 ? [...tsconfigPaths][0] : null,
    files,
    warnings,
    indexCache: repoIndex.cacheStats,
    partial:
      inventory.partial ||
      inventory.configurationPartial ||
      repoIndex.partial === true ||
      failedFiles.length > 0,
    failedFiles,
    shardFailures,
  };
}

interface ReferenceScanResult {
  directReferences: Reference[];
  consumerImpact: {
    references: Reference[];
    callees: Reference[];
  };
  completed: boolean;
}

function filterReferencesByInventory(
  references: Reference[],
  repo: string,
  inventory: RepoFileInventory,
  changedPathKeys: Set<string>,
): Reference[] {
  return references.filter((reference) => {
    const pathKey = canonicalPathKey(path.resolve(repo, reference.file));
    return inventory.pathKeys.has(pathKey) || changedPathKeys.has(pathKey);
  });
}

function collectReferenceScan(
  program: import("typescript").Program,
  checker: import("typescript").TypeChecker,
  symbol: CollectedSymbol,
  repo: string,
  includeConsumerImpact: boolean,
  shouldStop: () => boolean = () => false,
  sourceAllowed: (source: import("typescript").SourceFile) => boolean = () => true,
): ReferenceScanResult {
  try {
    const directReferences = collectReferences(
      program,
      checker,
      symbol,
      repo,
      REFERENCE_COLLECTION_LIMIT,
      shouldStop,
      sourceAllowed,
    );
    if (!shouldStop()) {
      directReferences.push(
        ...collectImplementedMemberUsageReferences(
          program,
          checker,
          symbol,
          repo,
          REFERENCE_COLLECTION_LIMIT,
          shouldStop,
          sourceAllowed,
        ),
      );
    }
    const consumerImpact = includeConsumerImpact && !shouldStop()
      ? collectReferenceConsumerImpact(
          program,
          checker,
          symbol,
          repo,
          REFERENCE_COLLECTION_LIMIT,
          shouldStop,
          sourceAllowed,
        )
      : { references: [], callees: [] };
    return { directReferences, consumerImpact, completed: !shouldStop() };
  } catch (error) {
    if (error instanceof ReferenceScanCancelled) {
      return { directReferences: [], consumerImpact: { references: [], callees: [] }, completed: false };
    }
    throw error;
  }
}

function syntheticReferenceScanCacheKey(symbol: CollectedSymbol): string | null {
  if (!symbol.analysis.name.includes(":") || !symbol.containerNode || !symbol.tsSymbol) return null;
  const source = symbol.containerNode.getSourceFile();
  return [
    source.fileName,
    symbol.containerNode.getStart(source),
    symbol.containerNode.getEnd(),
    symbol.analysis.name,
    symbol.analysis.startLine,
    symbol.analysis.endLine,
  ].join(":");
}

function analysisBudget(timeBudgetMs: number | null): () => boolean {
  if (timeBudgetMs === null) return () => false;
  const deadline = Date.now() + timeBudgetMs;
  return () => Date.now() >= deadline;
}
