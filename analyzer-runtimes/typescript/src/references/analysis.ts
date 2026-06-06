import path from "node:path";

import ts from "typescript";

import { calleeNameNode, propertyNameText } from "../ast-utils.js";
import { canonicalSymbol } from "../checker-utils.js";
import {
  declarationName,
  enclosingImpactDeclaration,
  isDeclarationInsideTarget,
  isNodeInsideTarget,
  symbolForDeclaration,
  symbolHasDeclarationContainingNode,
} from "../declaration-utils.js";
import { implementedMemberSymbols } from "../symbols/implemented-members.js";
import { addReference } from "./merge.js";
import { isPropertyAccessMemberName, isReferenceToTarget } from "./target-match.js";
import { isDeclarationNameIdentifier, referenceForNode, referenceKind } from "./utils.js";
import type { CollectedSymbol, Reference } from "../types.js";
import { isInsideRepo, isRepoRelativePath, normalizeRelPath, sourceFileName } from "../utils.js";

type ReferenceSearchText = string | readonly string[] | null;

export class ReferenceScanCancelled extends Error {
  constructor() {
    super("Reference scan cancelled");
    this.name = "ReferenceScanCancelled";
  }
}

export function collectReferences(
  program: ts.Program,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
  shouldStop: () => boolean = () => false,
): Reference[] {
  const refs: Reference[] = [];
  const seen = new Set<string>();
  const targetSource = target.node.getSourceFile();
  const excludedNode = target.containerNode ?? target.node;
  const targetStart = excludedNode.getStart(targetSource);
  const targetEnd = excludedNode.getEnd();
  const searchText = referenceSearchText(target);
  for (const source of program.getSourceFiles()) {
    stopIfNeeded(shouldStop);
    if (source.isDeclarationFile) continue;
    if (!isInsideRepo(repo, source.fileName)) continue;
    if (!sourceMayContainReference(source, searchText)) continue;
    visit(source);
    if (refs.length >= limit) break;
  }
  return refs;

  function visit(node: ts.Node): void {
    stopIfNeeded(shouldStop);
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

export function collectImplementedMemberUsageReferences(
  program: ts.Program,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
  shouldStop: () => boolean = () => false,
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
    stopIfNeeded(shouldStop);
    if (source.isDeclarationFile) continue;
    if (!isInsideRepo(repo, source.fileName)) continue;
    if (!sourceMayContainReference(source, methodName)) continue;
    visit(source);
    if (refs.length >= limit) break;
  }
  return refs;

  function visit(node: ts.Node): void {
    stopIfNeeded(shouldStop);
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

export function collectReferenceConsumerImpact(
  program: ts.Program,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
  shouldStop: () => boolean = () => false,
): { references: Reference[]; callees: Reference[] } {
  const references: Reference[] = [];
  const callees: Reference[] = [];
  const seenReferences = new Set<string>();
  const seenCallees = new Set<string>();
  const consumerSymbols = new Set<ts.Symbol>();
  const consumerNames = new Set<string>();
  const targetSource = target.node.getSourceFile();
  const excludedNode = target.containerNode ?? target.node;
  const searchText = referenceSearchText(target);

  for (const source of program.getSourceFiles()) {
    stopIfNeeded(shouldStop);
    if (source.isDeclarationFile || !isInsideRepo(repo, source.fileName)) continue;
    if (!sourceMayContainReference(source, searchText)) continue;
    collectConsumerSymbols(source);
  }

  for (const symbol of consumerSymbols) {
    stopIfNeeded(shouldStop);
    if (references.length >= limit && callees.length >= limit) break;
    collectConsumerReferences(symbol, null);
  }
  for (const name of consumerNames) {
    stopIfNeeded(shouldStop);
    if (references.length >= limit && callees.length >= limit) break;
    collectConsumerReferences(null, name);
  }

  return { references, callees };

  function collectConsumerSymbols(source: ts.SourceFile): void {
    visit(source);

    function visit(node: ts.Node): void {
      stopIfNeeded(shouldStop);
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
    const consumerSearchText = consumerSymbol === null ? consumerName : null;
    for (const source of program.getSourceFiles()) {
      stopIfNeeded(shouldStop);
      if (source.isDeclarationFile || !isInsideRepo(repo, source.fileName)) continue;
      if (!sourceMayContainReference(source, consumerSearchText)) continue;
      visit(source);
      if (references.length >= limit && callees.length >= limit) return;
    }

    function visit(node: ts.Node): void {
      stopIfNeeded(shouldStop);
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
              collectCalleesFromNode(checker, callerDeclaration, repo, limit, callees, seenCallees, shouldStop);
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

function referenceSearchText(target: CollectedSymbol): ReferenceSearchText {
  if (target.defaultExported) return null;
  const syntheticSeparatorIndex = target.analysis.name.indexOf(":");
  if (syntheticSeparatorIndex > 0) {
    const containerName = target.analysis.name.slice(0, syntheticSeparatorIndex);
    const memberName = target.analysis.name.slice(syntheticSeparatorIndex + 1);
    return [containerName, memberName].filter((value) => value.length > 0);
  }
  if (target.exportContainer || target.analysis.kind === "method" || target.analysis.kind === "enum-member") {
    return target.analysis.name ? [target.analysis.name] : null;
  }
  return null;
}

function sourceMayContainReference(source: ts.SourceFile, searchText: ReferenceSearchText): boolean {
  const candidates = typeof searchText === "string" ? [searchText] : searchText;
  return candidates === null || candidates.some((value) => source.text.includes(value)) || source.text.includes("\\u");
}

function stopIfNeeded(shouldStop: () => boolean): void {
  if (shouldStop()) {
    throw new ReferenceScanCancelled();
  }
}

export function collectCallees(
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
  shouldStop: () => boolean = () => false,
): Reference[] {
  const refs: Reference[] = [];
  const seen = new Set<string>();
  collectCalleesFromNode(checker, target.node, repo, limit, refs, seen, shouldStop, target.node);
  return refs;
}

function collectCalleesFromNode(
  checker: ts.TypeChecker,
  node: ts.Node,
  repo: string,
  limit: number,
  refs: Reference[],
  seen: Set<string>,
  shouldStop: () => boolean,
  excludedNode?: ts.Node,
): void {
  const targetSource = node.getSourceFile();
  visit(node);

  function visit(node: ts.Node): void {
    stopIfNeeded(shouldStop);
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
