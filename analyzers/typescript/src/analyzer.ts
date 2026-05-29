import path from "node:path";

import { collectFrameworkMetadata, collectSchemaContracts } from "./contracts/contract-analysis.js";
import { REFERENCE_COLLECTION_LIMIT, REFERENCE_LIMIT } from "./constants.js";
import {
  collectCallees,
  collectImplementedMemberUsageReferences,
  collectReferenceConsumerImpact,
  collectReferences,
} from "./references/reference-analysis.js";
import { mergeReferences } from "./references/reference-merge.js";
import { buildRepoIndex } from "./indexes/repo-index.js";
import { createProgramContexts } from "./program.js";
import {
  collectDeletedSymbols,
  collectExports,
  collectImports,
  collectSymbols,
  preferSyntheticChildSymbols,
} from "./symbols/symbol-collection.js";
import { findRelatedTests, isTestPath } from "./test-discovery.js";
import type {
  AnalyzerResult,
  Args,
  FileAnalysis,
} from "./types.js";
import { rangesOverlap } from "./utils.js";
import {
  collectProviderTokenInjectionReferences,
  collectWorkspaceDiReferences,
  collectWorkspaceImportReferences,
  collectWorkspaceMemberReferences,
  filterInvalidWorkspaceMemberReferences,
} from "./workspace/workspace-references.js";

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
  const contextsByFile = createProgramContexts(args, warnings);
  const repoIndex = buildRepoIndex(args);

  const files: FileAnalysis[] = [];
  for (const changedFile of args.changed) {
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

    for (const symbol of changedCollectedSymbols) {
      if (isChangedTestFile) {
        symbol.analysis.references = [];
        symbol.analysis.callees = [];
        symbol.analysis.contracts = [];
        symbol.analysis.metadata = [];
        continue;
      }
      const directReferences = [
        ...collectReferences(program, checker, symbol, args.repo, REFERENCE_COLLECTION_LIMIT),
        ...collectImplementedMemberUsageReferences(program, checker, symbol, args.repo, REFERENCE_COLLECTION_LIMIT),
      ];
      const consumerImpact = !symbol.analysis.name.includes(":")
        ? { references: [], callees: [] }
        : collectReferenceConsumerImpact(program, checker, symbol, args.repo, REFERENCE_COLLECTION_LIMIT);
      symbol.analysis.references = mergeReferences(
        [
          ...directReferences,
          ...consumerImpact.references,
          ...collectWorkspaceImportReferences(args.repo, repoIndex, symbol, REFERENCE_COLLECTION_LIMIT),
          ...collectWorkspaceMemberReferences(args.repo, repoIndex, symbol, REFERENCE_COLLECTION_LIMIT),
          ...collectWorkspaceDiReferences(args.repo, repoIndex, symbol, REFERENCE_COLLECTION_LIMIT),
          ...collectProviderTokenInjectionReferences(args.repo, repoIndex, symbol, REFERENCE_COLLECTION_LIMIT),
        ],
        REFERENCE_LIMIT,
      );
      symbol.analysis.references = filterInvalidWorkspaceMemberReferences(args.repo, repoIndex, symbol, symbol.analysis.references);
      symbol.analysis.callees = mergeReferences(
        [
          ...collectCallees(checker, symbol, args.repo, REFERENCE_COLLECTION_LIMIT),
          ...consumerImpact.callees,
        ],
        REFERENCE_LIMIT,
      );
      symbol.analysis.contracts = mergeReferences(
        collectSchemaContracts(program, checker, symbol, args.repo, REFERENCE_COLLECTION_LIMIT),
        REFERENCE_LIMIT,
      );
      symbol.analysis.metadata = mergeReferences(
        collectFrameworkMetadata(symbol, args.repo, REFERENCE_COLLECTION_LIMIT),
        REFERENCE_LIMIT,
      );
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
  };
}
