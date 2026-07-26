import path from "node:path";

import ts from "typescript";

import type {
  IdentifierIndexEntry,
  IndexedReference,
  Reference,
  ReferenceKind,
  RepoFileIndexEntry,
  RepoIndex,
} from "../types.js";
import { normalizeRelPath, readUtf8 } from "../utils.js";

const indexedSourceLines = new WeakMap<RepoIndex, Map<string, string[] | null>>();

export function referenceForIdentifier(repo: string, source: ts.SourceFile, node: ts.Identifier): Reference {
  return referenceForNode(repo, source, node, referenceKind(node));
}

export function referenceForNode(repo: string, source: ts.SourceFile, node: ts.Node, kind: ReferenceKind): Reference {
  const reference = indexedReferenceForNode(repo, source, node, kind);
  return materializedReference(reference, sourceLineText(source, reference.line - 1) ?? node.getText(source));
}

export function indexedReferenceForIdentifier(
  repo: string,
  source: ts.SourceFile,
  node: ts.Identifier,
): IndexedReference {
  return indexedReferenceForNode(repo, source, node, referenceKind(node));
}

export function indexedReferenceForNode(
  repo: string,
  source: ts.SourceFile,
  node: ts.Node,
  kind: ReferenceKind,
): IndexedReference {
  const position = source.getLineAndCharacterOfPosition(node.getStart(source));
  const endPosition = source.getLineAndCharacterOfPosition(node.getEnd());
  const reference: IndexedReference = {
    file: normalizeRelPath(path.relative(repo, source.fileName)),
    line: position.line + 1,
    kind,
  };
  if (endPosition.line > position.line) {
    reference.endLine = endPosition.line + 1;
  }
  return reference;
}

export function materializeIdentifierReference(
  repoIndex: RepoIndex,
  entry: RepoFileIndexEntry,
  identifier: IdentifierIndexEntry,
): Reference {
  let linesByFile = indexedSourceLines.get(repoIndex);
  if (!linesByFile) {
    linesByFile = new Map();
    indexedSourceLines.set(repoIndex, linesByFile);
  }
  let lines = linesByFile.get(entry.absPath);
  if (lines === undefined) {
    const text = readUtf8(entry.absPath);
    lines = text === null ? null : text.split(/\r?\n/);
    linesByFile.set(entry.absPath, lines);
  }
  return materializedReference(
    identifier.reference,
    lines?.[identifier.reference.line - 1]?.trim() ?? identifier.name,
  );
}

function materializedReference(reference: IndexedReference, text: string): Reference {
  const materialized: Reference = {
    file: reference.file,
    line: reference.line,
    text,
    kind: reference.kind,
  };
  if (reference.endLine !== undefined) {
    materialized.endLine = reference.endLine;
  }
  return materialized;
}

function sourceLineText(source: ts.SourceFile, zeroBasedLine: number): string | null {
  const lineStarts = source.getLineStarts();
  const start = lineStarts[zeroBasedLine];
  if (start === undefined) return null;
  let end = lineStarts[zeroBasedLine + 1] ?? source.text.length;
  while (end > start && (source.text[end - 1] === "\n" || source.text[end - 1] === "\r")) {
    end -= 1;
  }
  return source.text.slice(start, end).trim();
}

export function referenceKind(node: ts.Identifier): ReferenceKind {
  if (hasAncestor(node, ts.isImportDeclaration) || hasAncestor(node, ts.isImportSpecifier) || hasAncestor(node, ts.isImportClause)) {
    return "import";
  }
  if (isCallExpressionName(node)) {
    return "call";
  }
  if (isWriteReference(node)) {
    return "write";
  }
  if (isTypeReference(node)) {
    return "type";
  }
  return "read";
}

export function isDeclarationNameIdentifier(node: ts.Identifier): boolean {
  const parent = node.parent;
  return (
    (ts.isBindingElement(parent) && parent.name === node) ||
    (ts.isClassDeclaration(parent) && parent.name === node) ||
    (ts.isEnumDeclaration(parent) && parent.name === node) ||
    (ts.isEnumMember(parent) && parent.name === node) ||
    (ts.isFunctionDeclaration(parent) && parent.name === node) ||
    (ts.isImportClause(parent) && parent.name === node) ||
    (ts.isImportSpecifier(parent) && parent.name === node) ||
    (ts.isInterfaceDeclaration(parent) && parent.name === node) ||
    (ts.isMethodDeclaration(parent) && parent.name === node) ||
    (ts.isMethodSignature(parent) && parent.name === node) ||
    (ts.isParameter(parent) && parent.name === node) ||
    (ts.isPropertyAssignment(parent) && parent.name === node) ||
    (ts.isPropertyDeclaration(parent) && parent.name === node) ||
    (ts.isPropertySignature(parent) && parent.name === node) ||
    (ts.isTypeAliasDeclaration(parent) && parent.name === node) ||
    (ts.isVariableDeclaration(parent) && parent.name === node)
  );
}

export function hasAncestor<T extends ts.Node>(node: ts.Node, predicate: (node: ts.Node) => node is T): boolean {
  let current: ts.Node | undefined = node.parent;
  while (current) {
    if (predicate(current)) return true;
    current = current.parent;
  }
  return false;
}

function isCallExpressionName(node: ts.Identifier): boolean {
  const parent = node.parent;
  if (ts.isCallExpression(parent) && parent.expression === node) return true;
  if ((ts.isJsxSelfClosingElement(parent) || ts.isJsxOpeningElement(parent)) && parent.tagName === node) {
    return true;
  }
  if (
    ts.isPropertyAccessExpression(parent) &&
    parent.name === node &&
    ((ts.isCallExpression(parent.parent) && parent.parent.expression === parent) ||
      (ts.isJsxSelfClosingElement(parent.parent) && parent.parent.tagName === parent) ||
      (ts.isJsxOpeningElement(parent.parent) && parent.parent.tagName === parent))
  ) {
    return true;
  }
  return false;
}

function isWriteReference(node: ts.Identifier): boolean {
  const parent = node.parent;
  if (ts.isBinaryExpression(parent) && parent.left === node) return true;
  if (
    ts.isPropertyAccessExpression(parent) &&
    parent.name === node &&
    ts.isBinaryExpression(parent.parent) &&
    parent.parent.left === parent
  ) {
    return true;
  }
  if ((ts.isPrefixUnaryExpression(parent) || ts.isPostfixUnaryExpression(parent)) && parent.operand === node) return true;
  return false;
}

function isTypeReference(node: ts.Identifier): boolean {
  let current: ts.Node | undefined = node.parent;
  while (current) {
    if (
      ts.isTypeReferenceNode(current) ||
      ts.isExpressionWithTypeArguments(current) ||
      ts.isTypeQueryNode(current) ||
      ts.isHeritageClause(current)
    ) {
      return true;
    }
    if (ts.isStatement(current) || ts.isExpression(current)) {
      return false;
    }
    current = current.parent;
  }
  return false;
}
