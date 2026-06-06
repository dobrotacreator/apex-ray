import path from "node:path";

import { collectFrameworkMetadata, collectSchemaContracts } from "./contracts/analysis.js";
import { REFERENCE_COLLECTION_LIMIT, REFERENCE_LIMIT } from "./constants.js";
import {
  collectCallees,
  collectImplementedMemberUsageReferences,
  collectReferenceConsumerImpact,
  collectReferences,
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
import { rangesOverlap } from "./utils.js";
import {
  collectProviderTokenInjectionReferences,
  collectWorkspaceDiReferences,
  collectWorkspaceImportReferences,
  collectWorkspaceMemberReferences,
  filterInvalidWorkspaceMemberReferences,
} from "./workspace/references.js";

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
  const budget = analysisBudget(args.analysisTimeBudgetMs);
  const contextsByFile = createProgramContexts(args, warnings);
  const repoIndex = buildRepoIndex(args);
  const syntheticReferenceScanCache = new Map<string, ReferenceScanResult>();
  const failedFileSet = new Set<string>();
  const failedFiles: string[] = [];
  const shardFailures: AnalyzerShardFailure[] = [];

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
    if (budget.exhausted()) {
      markBudgetExhausted(args.changed.slice(changedIndex));
      break;
    }

    const context = contextsByFile.get(changedFile);
    if (!context) {
      warnings.push(`No TypeScript program could be created for changed file: ${changedFile}`);
      continue;
    }

    const { program, checker } = context;
    const absPath = path.resolve(args.repo, changedFile);
    const source = program.getSourceFile(absPath);
    if (!source) {
      warnings.push(`Changed file is not part of the TypeScript program: ${changedFile}`);
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

    for (let symbolIndex = 0; symbolIndex < changedCollectedSymbols.length; symbolIndex += 1) {
      if (budget.exhausted()) {
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
        collectReferenceScan(program, checker, symbol, args.repo, symbol.analysis.name.includes(":"), budget.exhausted);
      if (referenceScanCacheKey && !cachedReferenceScan) {
        syntheticReferenceScanCache.set(referenceScanCacheKey, referenceScan);
      }
      if (budget.exhausted()) {
        markBudgetExhausted(args.changed.slice(changedIndex));
        completedFile = false;
        break;
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
      if (budget.exhausted()) {
        markBudgetExhausted(args.changed.slice(changedIndex));
        completedFile = false;
        break;
      }

      symbol.analysis.callees = mergeReferences(
        [
          ...collectCallees(checker, symbol, args.repo, REFERENCE_COLLECTION_LIMIT, budget.exhausted),
          ...referenceScan.consumerImpact.callees,
        ],
        REFERENCE_LIMIT,
      );
      if (budget.exhausted()) {
        markBudgetExhausted(args.changed.slice(changedIndex));
        completedFile = false;
        break;
      }

      symbol.analysis.contracts = mergeReferences(
        collectSchemaContracts(program, checker, symbol, args.repo, REFERENCE_COLLECTION_LIMIT),
        REFERENCE_LIMIT,
      );
      if (budget.exhausted()) {
        markBudgetExhausted(args.changed.slice(changedIndex));
        completedFile = false;
        break;
      }

      symbol.analysis.metadata = mergeReferences(
        collectFrameworkMetadata(symbol, args.repo, REFERENCE_COLLECTION_LIMIT),
        REFERENCE_LIMIT,
      );
    }
    if (!completedFile) {
      break;
    }

    const changedReferences = changedCollectedSymbols.flatMap((symbol) => symbol.analysis.references);
    files.push({
      path: changedFile,
      tsconfigPath: context.tsconfigPath,
      symbols,
      imports,
      exports,
      relatedTests: findRelatedTests(args.repo, repoIndex, changedFile, changedReferences),
      changedSymbols: changedCollectedSymbols.map((symbol) => symbol.analysis),
    });
  }

  const tsconfigPaths = new Set(files.map((file) => file.tsconfigPath).filter((value): value is string => Boolean(value)));
  return {
    language: "typescript",
    projectRoot: args.repo,
    tsconfigPath: tsconfigPaths.size === 1 ? [...tsconfigPaths][0] : null,
    files,
    warnings,
    indexCache: repoIndex.cacheStats,
    partial: failedFiles.length > 0,
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
}

function collectReferenceScan(
  program: import("typescript").Program,
  checker: import("typescript").TypeChecker,
  symbol: CollectedSymbol,
  repo: string,
  includeConsumerImpact: boolean,
  shouldStop: () => boolean = () => false,
): ReferenceScanResult {
  const directReferences = collectReferences(program, checker, symbol, repo, REFERENCE_COLLECTION_LIMIT, shouldStop);
  if (!shouldStop()) {
    directReferences.push(
      ...collectImplementedMemberUsageReferences(program, checker, symbol, repo, REFERENCE_COLLECTION_LIMIT, shouldStop),
    );
  }
  const consumerImpact = includeConsumerImpact && !shouldStop()
    ? collectReferenceConsumerImpact(program, checker, symbol, repo, REFERENCE_COLLECTION_LIMIT, shouldStop)
    : { references: [], callees: [] };
  return { directReferences, consumerImpact };
}

function syntheticReferenceScanCacheKey(symbol: CollectedSymbol): string | null {
  if (!symbol.analysis.name.includes(":") || !symbol.containerNode || !symbol.tsSymbol) return null;
  const source = symbol.containerNode.getSourceFile();
  return `${source.fileName}:${symbol.containerNode.getStart(source)}:${symbol.containerNode.getEnd()}`;
}

function analysisBudget(timeBudgetMs: number | null): { exhausted: () => boolean } {
  if (timeBudgetMs === null) return { exhausted: () => false };
  const deadline = Date.now() + timeBudgetMs;
  return {
    exhausted: () => Date.now() >= deadline,
  };
}
