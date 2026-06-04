import path from "node:path";

import ts from "typescript";

import type { Reference, ReferenceKind } from "../types.js";
import { normalizeRelPath } from "../utils.js";

export function referenceForIdentifier(repo: string, source: ts.SourceFile, node: ts.Identifier): Reference {
  return referenceForNode(repo, source, node, referenceKind(node));
}

export function referenceForNode(repo: string, source: ts.SourceFile, node: ts.Node, kind: ReferenceKind): Reference {
  const position = source.getLineAndCharacterOfPosition(node.getStart(source));
  const endPosition = source.getLineAndCharacterOfPosition(node.getEnd());
  const reference: Reference = {
    file: normalizeRelPath(path.relative(repo, source.fileName)),
    line: position.line + 1,
    text: source.text.split(/\r?\n/)[position.line]?.trim() ?? node.getText(source),
    kind,
  };
  if (endPosition.line > position.line) {
    reference.endLine = endPosition.line + 1;
  }
  return reference;
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
