import ts from "typescript";

import { canonicalSymbol } from "./checker-utils.js";

export function declarationName(declaration: ts.Declaration): ts.Node | null {
  return ts.getNameOfDeclaration(declaration) ?? null;
}

export function isNodeInsideTarget(node: ts.Node, target: ts.Node, targetSource: ts.SourceFile): boolean {
  if (node.getSourceFile() !== targetSource) return false;
  const nodeStart = node.getStart(targetSource);
  return nodeStart >= target.getStart(targetSource) && nodeStart < target.getEnd();
}

export function isDeclarationInsideTarget(declaration: ts.Declaration, target: ts.Node, targetSource: ts.SourceFile): boolean {
  if (declaration.getSourceFile() !== targetSource) return false;
  const declarationStart = declaration.getStart(targetSource);
  return declarationStart >= target.getStart(targetSource) && declarationStart < target.getEnd();
}

export function symbolHasDeclarationContainingNode(symbol: ts.Symbol, node: ts.Node): boolean {
  for (const declaration of symbol.declarations ?? []) {
    if (isNodeInsideTarget(node, declaration, declaration.getSourceFile())) return true;
  }
  return false;
}

export function symbolForDeclaration(checker: ts.TypeChecker, declaration: ts.Declaration): ts.Symbol | null {
  const name = declarationName(declaration);
  return name ? canonicalSymbol(checker, checker.getSymbolAtLocation(name)) : null;
}

export function enclosingImpactDeclaration(node: ts.Node): ts.Declaration | null {
  let current: ts.Node | undefined = node;
  while (current) {
    if (
      ts.isMethodDeclaration(current) ||
      ts.isFunctionDeclaration(current) ||
      ts.isClassDeclaration(current) ||
      ts.isInterfaceDeclaration(current) ||
      ts.isTypeAliasDeclaration(current) ||
      ts.isEnumDeclaration(current)
    ) {
      return current;
    }
    if (ts.isVariableDeclaration(current) && ts.isIdentifier(current.name)) return current;
    if (
      (ts.isFunctionExpression(current) || ts.isArrowFunction(current)) &&
      ts.isVariableDeclaration(current.parent) &&
      ts.isIdentifier(current.parent.name)
    ) {
      return current.parent;
    }
    if (ts.isSourceFile(current)) return null;
    current = current.parent;
  }
  return null;
}
