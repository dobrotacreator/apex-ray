import path from "node:path";

import ts from "typescript";

import {
  calleeNameNode,
  decoratorsForNode,
  propertyNameText,
  unwrapExpression,
} from "./ast-utils.js";
import { canonicalSymbol } from "./checker-utils.js";
import { CLASS_HERITAGE_CONTRACT_DEPTH_LIMIT } from "./constants.js";
import { isDeclarationInsideTarget } from "./declaration-utils.js";
import { implementedMembers } from "./implemented-members.js";
import { addReference } from "./reference-merge.js";
import { referenceForNode } from "./reference-utils.js";
import {
  addContractDeclarationReferences,
  addContractSymbolWithDependencies,
} from "./contract-dependencies.js";
import {
  collectDecoratorMetadataKeyConsumerContracts,
  decoratorArgumentExpressions,
  decoratorNameNode,
} from "./contract-metadata.js";
import {
  metadataNodesForTarget,
  parametersForNode,
  returnTypeForNode,
  variableTypeNodesForTarget,
} from "./contract-targets.js";
import type { CollectedSymbol, Reference } from "./types.js";
import { isInsideRepo, isRepoRelativePath, normalizeRelPath } from "./utils.js";

export { collectFrameworkMetadata } from "./contract-metadata.js";

export function collectSchemaContracts(
  program: ts.Program,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): Reference[] {
  const refs: Reference[] = [];
  const seen = new Set<string>();
  const contractSymbolsSeen = new Set<ts.Symbol>();
  collectDeclaredTypeContracts(refs, seen, contractSymbolsSeen, checker, target, repo, limit);
  collectClassHeritageContracts(refs, seen, checker, target, repo, limit);
  collectDecoratorArgumentContracts(refs, seen, checker, target, repo, limit);
  collectDecoratorMetadataKeyConsumerContracts(refs, seen, program, checker, target, repo, limit);
  collectImplementedMemberContracts(refs, seen, checker, target, repo, limit);
  visit(target.node);
  return refs;

  function visit(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (ts.isCallExpression(node)) {
      const schemaNode = schemaReceiverNameNode(node.expression);
      const schemaSymbol = schemaNode ? canonicalSymbol(checker, checker.getSymbolAtLocation(schemaNode)) : null;
      addContractSymbolWithDependencies(refs, seen, contractSymbolsSeen, checker, schemaSymbol, target.node, repo, limit, 0);
      for (const argument of node.arguments) {
        if (refs.length >= limit) break;
        const schemaArgumentNode = schemaArgumentNameNode(argument);
        const schemaArgumentSymbol = schemaArgumentNode
          ? canonicalSymbol(checker, checker.getSymbolAtLocation(schemaArgumentNode))
          : null;
        addContractSymbolWithDependencies(refs, seen, contractSymbolsSeen, checker, schemaArgumentSymbol, target.node, repo, limit, 0);
      }
    }
    ts.forEachChild(node, visit);
  }
}

function collectClassHeritageContracts(
  refs: Reference[],
  seen: Set<string>,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): void {
  if (!ts.isClassDeclaration(target.node)) return;
  const seenSymbols = new Set<ts.Symbol>();
  collectHeritageDeclarations(target.node, 0);

  function collectHeritageDeclarations(
    declaration: ts.ClassDeclaration | ts.InterfaceDeclaration,
    depth: number,
  ): void {
    if (depth >= CLASS_HERITAGE_CONTRACT_DEPTH_LIMIT || refs.length >= limit) return;

    for (const clause of declaration.heritageClauses ?? []) {
      if (refs.length >= limit) return;
      if (clause.token !== ts.SyntaxKind.ImplementsKeyword && clause.token !== ts.SyntaxKind.ExtendsKeyword) continue;

      for (const heritageType of clause.types) {
        if (refs.length >= limit) return;
        const contractSymbol = symbolForHeritageType(checker, heritageType);
        if (!contractSymbol || seenSymbols.has(contractSymbol)) continue;
        seenSymbols.add(contractSymbol);
        addContractDeclarationReferences(refs, seen, contractSymbol, target.node, repo, limit);

        for (const contractDeclaration of contractSymbol.declarations ?? []) {
          if (refs.length >= limit) return;
          if (ts.isClassDeclaration(contractDeclaration) || ts.isInterfaceDeclaration(contractDeclaration)) {
            collectHeritageDeclarations(contractDeclaration, depth + 1);
          }
        }
      }
    }
  }
}

function symbolForHeritageType(checker: ts.TypeChecker, heritageType: ts.ExpressionWithTypeArguments): ts.Symbol | null {
  const expressionSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(heritageType.expression));
  if (expressionSymbol) return expressionSymbol;
  return canonicalSymbol(checker, checker.getTypeAtLocation(heritageType).symbol);
}

function collectDecoratorArgumentContracts(
  refs: Reference[],
  seen: Set<string>,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): void {
  for (const node of metadataNodesForTarget(target)) {
    if (refs.length >= limit) return;
    for (const decorator of decoratorsForNode(node)) {
      if (refs.length >= limit) return;
      const decoratorName = decoratorNameNode(decorator);
      const decoratorSymbol = decoratorName ? canonicalSymbol(checker, checker.getSymbolAtLocation(decoratorName)) : null;
      addContractDeclarationReferences(refs, seen, decoratorSymbol, target.node, repo, limit);
      for (const argument of decoratorArgumentExpressions(decorator)) {
        visitArgument(argument);
      }
    }
  }

  function visitArgument(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (ts.isIdentifier(node)) {
      const symbol = canonicalSymbol(checker, checker.getSymbolAtLocation(node));
      addContractDeclarationReferences(refs, seen, symbol, target.node, repo, limit);
    }
    ts.forEachChild(node, visitArgument);
  }
}

function collectDeclaredTypeContracts(
  refs: Reference[],
  seen: Set<string>,
  contractSymbolsSeen: Set<ts.Symbol>,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): void {
  for (const parameter of parametersForNode(target.node)) {
    if (!parameter.type) continue;
    visitType(parameter.type);
    if (refs.length >= limit) return;
  }

  const returnType = returnTypeForNode(target.node);
  if (returnType) {
    visitType(returnType);
  }

  for (const typeNode of variableTypeNodesForTarget(target)) {
    if (refs.length >= limit) return;
    visitType(typeNode);
  }

  function visitType(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (ts.isTypeReferenceNode(node)) {
      const symbolNode = entityNameLeaf(node.typeName);
      const typeSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(symbolNode));
      addContractSymbolWithDependencies(refs, seen, contractSymbolsSeen, checker, typeSymbol, target.node, repo, limit, 0);
    }
    if (ts.isTypeQueryNode(node)) {
      const symbolNode = entityNameLeaf(node.exprName);
      const valueSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(symbolNode));
      addContractSymbolWithDependencies(refs, seen, contractSymbolsSeen, checker, valueSymbol, target.node, repo, limit, 0);
    }
    ts.forEachChild(node, visitType);
  }
}

function collectImplementedMemberContracts(
  refs: Reference[],
  seen: Set<string>,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): void {
  if (!ts.isMethodDeclaration(target.node)) return;
  const methodName = propertyNameText(target.node.name);
  if (!methodName) return;

  for (const member of implementedMembers(checker, target)) {
    if (refs.length >= limit) return;
    for (const declaration of member.declarations ?? []) {
      if (refs.length >= limit) return;
      if (isDeclarationInsideTarget(declaration, target.node, target.node.getSourceFile())) continue;
      const source = declaration.getSourceFile();
      if (source.isDeclarationFile || !isInsideRepo(repo, source.fileName)) continue;
      const file = normalizeRelPath(path.relative(repo, source.fileName));
      if (!isRepoRelativePath(file)) continue;
      addReference(refs, seen, referenceForNode(repo, source, declaration, "contract"), limit);
    }
  }
}

function entityNameLeaf(name: ts.EntityName): ts.Identifier {
  return ts.isQualifiedName(name) ? name.right : name;
}

function schemaReceiverNameNode(expression: ts.Expression): ts.Node | null {
  if (!ts.isPropertyAccessExpression(expression)) return null;
  if (expression.name.text !== "parse" && expression.name.text !== "safeParse") return null;
  const receiver = expression.expression;
  if (ts.isIdentifier(receiver)) return receiver;
  if (ts.isPropertyAccessExpression(receiver)) return receiver.name;
  if (ts.isCallExpression(receiver)) return calleeNameNode(receiver.expression);
  return null;
}

function schemaArgumentNameNode(expression: ts.Expression): ts.Node | null {
  const unwrapped = unwrapExpression(expression);
  if (!unwrapped) return null;
  if (ts.isIdentifier(unwrapped) && isSchemaLikeName(unwrapped.text)) return unwrapped;
  if (ts.isPropertyAccessExpression(unwrapped) && isSchemaLikeName(unwrapped.name.text)) return unwrapped.name;
  return null;
}

function isSchemaLikeName(name: string): boolean {
  return name.endsWith("Schema");
}
