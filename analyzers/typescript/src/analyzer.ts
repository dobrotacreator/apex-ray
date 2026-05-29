import ts from "typescript";
import path from "node:path";

import { calleeNameNode, propertyNameText } from "./ast-utils.js";
import { canonicalSymbol } from "./checker-utils.js";
import { collectFrameworkMetadata, collectSchemaContracts } from "./contract-analysis.js";
import { REFERENCE_COLLECTION_LIMIT, REFERENCE_LIMIT } from "./constants.js";
import {
  declarationName,
  enclosingImpactDeclaration,
  isDeclarationInsideTarget,
  isNodeInsideTarget,
  symbolForDeclaration,
  symbolHasDeclarationContainingNode,
} from "./declaration-utils.js";
import { implementedMemberSymbols } from "./implemented-members.js";
import { createProgramContexts } from "./program.js";
import {
  isDeclarationNameIdentifier,
  referenceForNode,
  referenceKind,
} from "./reference-utils.js";
import { addReference, mergeReferences } from "./reference-merge.js";
import { buildRepoIndex } from "./repo-index.js";
import {
  collectDeletedSymbols,
  collectExports,
  collectImports,
  collectSymbols,
  preferSyntheticChildSymbols,
} from "./symbol-collection.js";
import { findRelatedTests, isTestPath } from "./test-discovery.js";
import type {
  AnalyzerResult,
  Args,
  CollectedSymbol,
  FileAnalysis,
  Reference,
  ReferenceKind,
} from "./types.js";
import {
  formatDiagnostic,
  isInsideRepo,
  isRepoRelativePath,
  normalizeRelPath,
  rangesOverlap,
  sourceFileName,
  walk,
} from "./utils.js";
import {
  collectProviderTokenInjectionReferences,
  collectWorkspaceDiReferences,
  collectWorkspaceImportReferences,
  collectWorkspaceMemberReferences,
  filterInvalidWorkspaceMemberReferences,
} from "./workspace-references.js";

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

function collectReferences(
  program: ts.Program,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): Reference[] {
  const refs: Reference[] = [];
  const seen = new Set<string>();
  const targetSource = target.node.getSourceFile();
  const excludedNode = target.containerNode ?? target.node;
  const targetStart = excludedNode.getStart(targetSource);
  const targetEnd = excludedNode.getEnd();
  for (const source of program.getSourceFiles()) {
    if (source.isDeclarationFile) continue;
    if (!isInsideRepo(repo, source.fileName)) continue;
    visit(source);
    if (refs.length >= limit) break;
  }
  return refs;

  function visit(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (ts.isIdentifier(node) && isReferenceToTarget(node, checker, target)) {
      const source = node.getSourceFile();
      if (sourceFileName(source) === sourceFileName(targetSource)) {
        const start = node.getStart(source);
        if (start >= targetStart && start < targetEnd) {
          ts.forEachChild(node, visit);
          return;
        }
      }
      const position = source.getLineAndCharacterOfPosition(node.getStart(source));
      const file = normalizeRelPath(path.relative(repo, source.fileName));
      if (!isRepoRelativePath(file)) {
        ts.forEachChild(node, visit);
        return;
      }
      const text = source.text.split(/\r?\n/)[position.line]?.trim() ?? node.text;
      const kind = referenceKind(node);
      const key = `${file}:${position.line + 1}:${kind}:${text}`;
      if (!seen.has(key)) {
        seen.add(key);
        refs.push({
          file,
          line: position.line + 1,
          text,
          kind,
        });
      }
    }
    ts.forEachChild(node, visit);
  }
}

function collectImplementedMemberUsageReferences(
  program: ts.Program,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): Reference[] {
  if (!ts.isMethodDeclaration(target.node)) return [];
  const methodName = propertyNameText(target.node.name);
  if (!methodName) return [];

  const memberSymbols = implementedMemberSymbols(checker, target);
  if (memberSymbols.size === 0) return [];

  const refs: Reference[] = [];
  const seen = new Set<string>();
  const targetSource = target.node.getSourceFile();
  const excludedNode = target.containerNode ?? target.node;
  const targetStart = excludedNode.getStart(targetSource);
  const targetEnd = excludedNode.getEnd();

  for (const source of program.getSourceFiles()) {
    if (source.isDeclarationFile) continue;
    if (!isInsideRepo(repo, source.fileName)) continue;
    visit(source);
    if (refs.length >= limit) break;
  }
  return refs;

  function visit(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (ts.isIdentifier(node) && node.text === methodName && !isDeclarationNameIdentifier(node)) {
      const source = node.getSourceFile();
      if (sourceFileName(source) === sourceFileName(targetSource)) {
        const start = node.getStart(source);
        if (start >= targetStart && start < targetEnd) {
          ts.forEachChild(node, visit);
          return;
        }
      }
      const nodeSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(node));
      if (nodeSymbol && memberSymbols.has(nodeSymbol)) {
        const file = normalizeRelPath(path.relative(repo, source.fileName));
        if (isRepoRelativePath(file)) {
          addReference(refs, seen, referenceForNode(repo, source, node, referenceKind(node)), limit);
        }
      }
    }
    ts.forEachChild(node, visit);
  }
}

function collectReferenceConsumerImpact(
  program: ts.Program,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): { references: Reference[]; callees: Reference[] } {
  const references: Reference[] = [];
  const callees: Reference[] = [];
  const seenReferences = new Set<string>();
  const seenCallees = new Set<string>();
  const consumerSymbols = new Set<ts.Symbol>();
  const consumerNames = new Set<string>();
  const targetSource = target.node.getSourceFile();
  const excludedNode = target.containerNode ?? target.node;

  for (const source of program.getSourceFiles()) {
    if (source.isDeclarationFile || !isInsideRepo(repo, source.fileName)) continue;
    collectConsumerSymbols(source);
  }

  for (const symbol of consumerSymbols) {
    if (references.length >= limit && callees.length >= limit) break;
    collectConsumerReferences(symbol, null);
  }
  for (const name of consumerNames) {
    if (references.length >= limit && callees.length >= limit) break;
    collectConsumerReferences(null, name);
  }

  return { references, callees };

  function collectConsumerSymbols(source: ts.SourceFile): void {
    visit(source);

    function visit(node: ts.Node): void {
      if (ts.isIdentifier(node) && isReferenceToTarget(node, checker, target)) {
        if (isNodeInsideTarget(node, excludedNode, targetSource)) {
          ts.forEachChild(node, visit);
          return;
        }
        const declaration = enclosingImpactDeclaration(node);
        const symbol = declaration ? symbolForDeclaration(checker, declaration) : null;
        const name = declaration ? declarationName(declaration) : null;
        const nameText = name ? name.getText(declaration?.getSourceFile()) : null;
        const sameNameAsTarget = nameText === target.analysis.name;
        if (declaration && symbol && !sameNameAsTarget && !isDeclarationInsideTarget(declaration, excludedNode, targetSource)) {
          consumerSymbols.add(symbol);
        }
        if (declaration && name && !sameNameAsTarget && !isDeclarationInsideTarget(declaration, excludedNode, targetSource)) {
          consumerNames.add(name.getText(declaration.getSourceFile()));
        }
      }
      ts.forEachChild(node, visit);
    }
  }

  function collectConsumerReferences(consumerSymbol: ts.Symbol | null, consumerName: string | null): void {
    for (const source of program.getSourceFiles()) {
      if (source.isDeclarationFile || !isInsideRepo(repo, source.fileName)) continue;
      visit(source);
      if (references.length >= limit && callees.length >= limit) return;
    }

    function visit(node: ts.Node): void {
      if (references.length < limit && ts.isIdentifier(node) && !isDeclarationNameIdentifier(node)) {
        const nodeSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(node));
        const matchesSymbol =
          consumerSymbol !== null && nodeSymbol === consumerSymbol && !symbolHasDeclarationContainingNode(consumerSymbol, node);
        const matchesName = consumerName !== null && node.text === consumerName;
        if ((matchesSymbol || matchesName) && isValidConsumerReferenceNode(node)) {
          const source = node.getSourceFile();
          const file = normalizeRelPath(path.relative(repo, source.fileName));
          if (isRepoRelativePath(file)) {
            addReference(references, seenReferences, referenceForNode(repo, source, node, referenceKind(node)), limit);
            const callerDeclaration = enclosingImpactDeclaration(node);
            const callerSymbol = callerDeclaration ? symbolForDeclaration(checker, callerDeclaration) : null;
            const callerName = callerDeclaration ? declarationName(callerDeclaration)?.getText(callerDeclaration.getSourceFile()) : null;
            if (
              callerDeclaration &&
              (consumerSymbol === null || callerSymbol !== consumerSymbol) &&
              (consumerName === null || callerName !== consumerName)
            ) {
              collectCalleesFromNode(checker, callerDeclaration, repo, limit, callees, seenCallees);
            }
          }
        }
      }
      ts.forEachChild(node, visit);
    }
  }

  function isValidConsumerReferenceNode(node: ts.Identifier): boolean {
    if (!isPropertyAccessMemberName(node)) return true;
    if (node.text !== target.analysis.name) return true;
    return isReferenceToTarget(node, checker, target);
  }
}

function collectCallees(
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): Reference[] {
  const refs: Reference[] = [];
  const seen = new Set<string>();
  collectCalleesFromNode(checker, target.node, repo, limit, refs, seen, target.node);
  return refs;
}

function collectCalleesFromNode(
  checker: ts.TypeChecker,
  node: ts.Node,
  repo: string,
  limit: number,
  refs: Reference[],
  seen: Set<string>,
  excludedNode?: ts.Node,
): void {
  const targetSource = node.getSourceFile();
  visit(node);

  function visit(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (ts.isCallExpression(node)) {
      const calleeNode = calleeNameNode(node.expression);
      const calleeSymbol = calleeNode ? canonicalSymbol(checker, checker.getSymbolAtLocation(calleeNode)) : null;
      if (calleeSymbol) {
        for (const declaration of calleeSymbol.declarations ?? []) {
          if (refs.length >= limit) break;
          if (excludedNode && isDeclarationInsideTarget(declaration, excludedNode, targetSource)) continue;
          const source = declaration.getSourceFile();
          if (source.isDeclarationFile || !isInsideRepo(repo, source.fileName)) continue;
          const file = normalizeRelPath(path.relative(repo, source.fileName));
          if (!isRepoRelativePath(file)) continue;
          addReference(refs, seen, referenceForNode(repo, source, declaration, "callee"), limit);
        }
      }
    }
    ts.forEachChild(node, visit);
  }
}

function isReferenceToTarget(node: ts.Identifier, checker: ts.TypeChecker, target: CollectedSymbol): boolean {
  if (target.tsSymbol) {
    const nodeSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(node));
    if (nodeSymbol !== target.tsSymbol) return false;
    if (target.exportContainer && isPropertyAccessMemberName(node)) {
      return memberReceiverMatchesExportContainer(node, checker, target);
    }
    return true;
  }
  return node.text === target.analysis.name;
}

function isPropertyAccessMemberName(node: ts.Identifier): boolean {
  return ts.isPropertyAccessExpression(node.parent) && node.parent.name === node;
}

function memberReceiverMatchesExportContainer(
  node: ts.Identifier,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
): boolean {
  const parent = node.parent;
  if (!ts.isPropertyAccessExpression(parent) || parent.name !== node) return true;
  const receiverType = checker.getTypeAtLocation(parent.expression);
  return typeMatchesTargetContainer(receiverType, checker, target);
}

function typeMatchesTargetContainer(type: ts.Type, checker: ts.TypeChecker, target: CollectedSymbol): boolean {
  const container = target.containerNode ?? target.node.parent;
  if (!container) return false;
  const apparentType = checker.getApparentType(type);
  const candidates = [type.getSymbol(), type.aliasSymbol, apparentType.getSymbol(), apparentType.aliasSymbol];
  return candidates.some((symbol) => symbolMatchesOrExtendsNode(symbol, container, checker));
}

function symbolHasDeclarationMatchingNode(symbol: ts.Symbol | undefined, node: ts.Node): boolean {
  if (!symbol) return false;
  return (symbol.declarations ?? []).some((declaration) => isSameNodeRange(declaration, node));
}

function symbolMatchesOrExtendsNode(symbol: ts.Symbol | undefined, node: ts.Node, checker: ts.TypeChecker): boolean {
  if (!symbol) return false;
  return (symbol.declarations ?? []).some((declaration) =>
    declarationMatchesOrExtendsNode(declaration, node, checker, new Set()),
  );
}

function declarationMatchesOrExtendsNode(
  declaration: ts.Declaration,
  node: ts.Node,
  checker: ts.TypeChecker,
  seen: Set<string>,
): boolean {
  if (isSameNodeRange(declaration, node)) return true;
  if (!isHeritageDeclaration(declaration)) return false;

  const key = `${declaration.getSourceFile().fileName}:${declaration.getStart(declaration.getSourceFile())}:${declaration.getEnd()}`;
  if (seen.has(key)) return false;
  seen.add(key);

  for (const clause of declaration.heritageClauses ?? []) {
    for (const heritageType of clause.types) {
      const heritageSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(heritageType.expression));
      if (!heritageSymbol) continue;
      for (const heritageDeclaration of heritageSymbol.declarations ?? []) {
        if (declarationMatchesOrExtendsNode(heritageDeclaration, node, checker, seen)) return true;
      }
    }
  }
  return false;
}

function isHeritageDeclaration(node: ts.Node): node is ts.ClassDeclaration | ts.InterfaceDeclaration {
  return ts.isClassDeclaration(node) || ts.isInterfaceDeclaration(node);
}

function isSameNodeRange(left: ts.Node, right: ts.Node): boolean {
  return (
    left.getSourceFile().fileName === right.getSourceFile().fileName &&
    left.getStart(left.getSourceFile()) === right.getStart(right.getSourceFile()) &&
    left.getEnd() === right.getEnd()
  );
}
