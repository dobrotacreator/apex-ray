import ts from "typescript";

import { canonicalSymbol } from "./checker-utils.js";
import type { CollectedSymbol } from "./types.js";

export function isReferenceToTarget(node: ts.Identifier, checker: ts.TypeChecker, target: CollectedSymbol): boolean {
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

export function isPropertyAccessMemberName(node: ts.Identifier): boolean {
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
