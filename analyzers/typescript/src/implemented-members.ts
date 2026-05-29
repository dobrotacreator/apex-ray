import ts from "typescript";

import { propertyNameText } from "./ast-utils.js";
import { canonicalSymbol } from "./checker-utils.js";
import { declarationName } from "./declaration-utils.js";
import type { CollectedSymbol } from "./types.js";

export function implementedMemberSymbols(checker: ts.TypeChecker, target: CollectedSymbol): Set<ts.Symbol> {
  const symbols = new Set<ts.Symbol>();
  for (const member of implementedMembers(checker, target)) {
    const memberSymbol = canonicalSymbol(checker, member);
    if (memberSymbol) symbols.add(memberSymbol);
    for (const declaration of member.declarations ?? []) {
      const name = declarationName(declaration);
      if (!name) continue;
      const declarationSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(name));
      if (declarationSymbol) symbols.add(declarationSymbol);
    }
  }
  return symbols;
}

export function implementedMembers(checker: ts.TypeChecker, target: CollectedSymbol): ts.Symbol[] {
  if (!ts.isMethodDeclaration(target.node)) return [];
  const methodName = propertyNameText(target.node.name);
  if (!methodName) return [];
  const parent = target.node.parent;
  if (!ts.isClassDeclaration(parent)) return [];

  const members: ts.Symbol[] = [];
  for (const clause of parent.heritageClauses ?? []) {
    if (clause.token !== ts.SyntaxKind.ImplementsKeyword && clause.token !== ts.SyntaxKind.ExtendsKeyword) continue;
    for (const heritageType of clause.types) {
      const contractType = checker.getTypeAtLocation(heritageType);
      const member = contractType.getProperty(methodName);
      if (member) members.push(member);
    }
  }
  return members;
}
