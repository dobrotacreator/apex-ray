import path from "node:path";

import ts from "typescript";

import {
  calleeNameNode,
  decoratorsForNode,
  entityNameText,
  propertyNameText,
  unwrapExpression,
} from "./ast-utils.js";
import { canonicalSymbol } from "./checker-utils.js";
import {
  CLASS_HERITAGE_CONTRACT_DEPTH_LIMIT,
  CONTRACT_DEPENDENCY_DEPTH_LIMIT,
  IGNORED_CONTRACT_DEPENDENCY_NAMES,
} from "./constants.js";
import { isDeclarationInsideTarget } from "./declaration-utils.js";
import { implementedMembers } from "./implemented-members.js";
import { addReference } from "./reference-merge.js";
import { hasAncestor, referenceForNode } from "./reference-utils.js";
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

function addContractDeclarationReferences(
  refs: Reference[],
  seen: Set<string>,
  symbol: ts.Symbol | null,
  targetNode: ts.Node,
  repo: string,
  limit: number,
): void {
  if (!symbol) return;
  for (const declaration of contractDeclarationsForSymbol(symbol, targetNode, repo)) {
    if (refs.length >= limit) break;
    addReference(refs, seen, referenceForNode(repo, declaration.getSourceFile(), declaration, "contract"), limit);
  }
}

function addContractSymbolWithDependencies(
  refs: Reference[],
  seen: Set<string>,
  contractSymbolsSeen: Set<ts.Symbol>,
  checker: ts.TypeChecker,
  symbol: ts.Symbol | null,
  targetNode: ts.Node,
  repo: string,
  limit: number,
  depth: number,
): void {
  if (!symbol || refs.length >= limit) return;
  const firstVisit = !contractSymbolsSeen.has(symbol);
  if (firstVisit) {
    contractSymbolsSeen.add(symbol);
  }

  const declarations = contractDeclarationsForSymbol(symbol, targetNode, repo);
  for (const declaration of declarations) {
    if (refs.length >= limit) break;
    addReference(refs, seen, referenceForNode(repo, declaration.getSourceFile(), declaration, "contract"), limit);
  }

  if (!firstVisit || depth >= CONTRACT_DEPENDENCY_DEPTH_LIMIT) return;
  for (const declaration of declarations) {
    collectContractDeclarationDependencies(
      refs,
      seen,
      contractSymbolsSeen,
      checker,
      declaration,
      targetNode,
      repo,
      limit,
      depth + 1,
    );
  }
}

function contractDeclarationsForSymbol(symbol: ts.Symbol, targetNode: ts.Node, repo: string): ts.Declaration[] {
  const declarations: ts.Declaration[] = [];
  for (const declaration of symbol.declarations ?? []) {
    if (isDeclarationInsideTarget(declaration, targetNode, targetNode.getSourceFile())) continue;
    const source = declaration.getSourceFile();
    if (source.isDeclarationFile || !isInsideRepo(repo, source.fileName)) continue;
    const file = normalizeRelPath(path.relative(repo, source.fileName));
    if (!isRepoRelativePath(file)) continue;
    declarations.push(declaration);
  }
  return declarations;
}

function collectContractDeclarationDependencies(
  refs: Reference[],
  seen: Set<string>,
  contractSymbolsSeen: Set<ts.Symbol>,
  checker: ts.TypeChecker,
  declaration: ts.Declaration,
  targetNode: ts.Node,
  repo: string,
  limit: number,
  depth: number,
): void {
  const pickedPropertyNamesBySymbol = pickedPropertyNamesByDependencySymbol(declaration, checker);
  visit(declaration);

  function visit(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (node !== declaration && isContractDependencyTraversalBoundary(node)) return;
    if (ts.isIdentifier(node) && isContractDependencyIdentifier(node)) {
      const dependencySymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(node));
      if (dependencySymbol && !symbolHasDeclarationInside(dependencySymbol, declaration)) {
        addContractSymbolWithDependencies(
          refs,
          seen,
          contractSymbolsSeen,
          checker,
          dependencySymbol,
          targetNode,
          repo,
          limit,
          depth,
        );
        const pickedPropertyNames = pickedPropertyNamesBySymbol.get(dependencySymbol);
        if (pickedPropertyNames) {
          addPickedPropertyContractReferences(
            refs,
            seen,
            checker,
            dependencySymbol,
            pickedPropertyNames,
            targetNode,
            repo,
            limit,
            depth,
            new Set(),
          );
        }
      }
    }
    ts.forEachChild(node, visit);
  }
}

function pickedPropertyNamesByDependencySymbol(
  declaration: ts.Declaration,
  checker: ts.TypeChecker,
): Map<ts.Symbol, Set<string>> {
  const namesBySymbol = new Map<ts.Symbol, Set<string>>();
  visit(declaration);
  return namesBySymbol;

  function visit(node: ts.Node): void {
    if (ts.isTypeReferenceNode(node) && entityNameText(node.typeName) === "Pick" && node.typeArguments?.[0] && node.typeArguments[1]) {
      const baseSymbol = symbolForTypeNode(checker, node.typeArguments[0]);
      if (!baseSymbol) {
        ts.forEachChild(node, visit);
        return;
      }
      const names = namesBySymbol.get(baseSymbol) ?? new Set<string>();
      collectStringLiteralTypeNames(node.typeArguments[1], names);
      namesBySymbol.set(baseSymbol, names);
    }
    ts.forEachChild(node, visit);
  }
}

function symbolForTypeNode(checker: ts.TypeChecker, typeNode: ts.TypeNode): ts.Symbol | null {
  if (ts.isTypeReferenceNode(typeNode)) {
    return canonicalSymbol(checker, checker.getSymbolAtLocation(typeNode.typeName));
  }
  if (ts.isTypeQueryNode(typeNode)) {
    return canonicalSymbol(checker, checker.getSymbolAtLocation(typeNode.exprName));
  }
  const type = checker.getTypeAtLocation(typeNode);
  return canonicalSymbol(checker, type.aliasSymbol ?? type.getSymbol());
}

function collectStringLiteralTypeNames(node: ts.Node, names: Set<string>): void {
  if (ts.isLiteralTypeNode(node) && ts.isStringLiteral(node.literal)) {
    names.add(node.literal.text);
    return;
  }
  ts.forEachChild(node, (child) => collectStringLiteralTypeNames(child, names));
}

function addPickedPropertyContractReferences(
  refs: Reference[],
  seen: Set<string>,
  checker: ts.TypeChecker,
  symbol: ts.Symbol | null,
  propertyNames: Set<string>,
  targetNode: ts.Node,
  repo: string,
  limit: number,
  depth: number,
  visited: Set<ts.Symbol>,
): void {
  if (!symbol || propertyNames.size === 0 || refs.length >= limit || depth > CONTRACT_DEPENDENCY_DEPTH_LIMIT) return;
  if (visited.has(symbol)) return;
  visited.add(symbol);

  const declarations = contractDeclarationsForSymbol(symbol, targetNode, repo);
  for (const declaration of declarations) {
    if (refs.length >= limit) return;
    collectPickedPropertyAssignments(refs, seen, declaration, propertyNames, repo, limit);
  }

  if (depth >= CONTRACT_DEPENDENCY_DEPTH_LIMIT) return;

  for (const declaration of declarations) {
    if (refs.length >= limit) return;
    visitDependencies(declaration, declaration);
  }

  function visitDependencies(node: ts.Node, root: ts.Declaration): void {
    if (refs.length >= limit) return;
    if (node !== root && isContractDependencyTraversalBoundary(node)) return;
    if (ts.isIdentifier(node) && isContractDependencyIdentifier(node)) {
      const dependencySymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(node));
      if (dependencySymbol && !symbolHasDeclarationInside(dependencySymbol, root)) {
        addPickedPropertyContractReferences(
          refs,
          seen,
          checker,
          dependencySymbol,
          propertyNames,
          targetNode,
          repo,
          limit,
          depth + 1,
          visited,
        );
      }
    }
    ts.forEachChild(node, (child) => visitDependencies(child, root));
  }
}

function collectPickedPropertyAssignments(
  refs: Reference[],
  seen: Set<string>,
  declaration: ts.Declaration,
  propertyNames: Set<string>,
  repo: string,
  limit: number,
): void {
  visit(declaration);

  function visit(node: ts.Node): void {
    if (refs.length >= limit) return;
    const name = ts.isPropertyAssignment(node) ? propertyNameText(node.name) : null;
    if (name && propertyNames.has(name)) {
      addReference(refs, seen, referenceForNode(repo, node.getSourceFile(), node, "contract"), limit);
      return;
    }
    ts.forEachChild(node, visit);
  }
}

function isContractDependencyTraversalBoundary(node: ts.Node): boolean {
  return (
    ts.isFunctionDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isArrowFunction(node) ||
    ts.isMethodDeclaration(node) ||
    ts.isConstructorDeclaration(node) ||
    ts.isGetAccessorDeclaration(node) ||
    ts.isSetAccessorDeclaration(node)
  );
}

function isContractDependencyIdentifier(node: ts.Identifier): boolean {
  if (IGNORED_CONTRACT_DEPENDENCY_NAMES.has(node.text)) return false;
  if (hasAncestor(node, ts.isImportDeclaration) || hasAncestor(node, ts.isImportSpecifier) || hasAncestor(node, ts.isImportClause)) {
    return false;
  }
  const parent = node.parent;
  if (ts.isPropertyAccessExpression(parent) && parent.name === node) return false;
  if (ts.isQualifiedName(parent) && parent.right === node) return false;
  if (ts.isPropertyAssignment(parent) && parent.name === node) return false;
  if (ts.isPropertyDeclaration(parent) && parent.name === node) return false;
  if (ts.isMethodDeclaration(parent) && parent.name === node) return false;
  if (ts.isParameter(parent) && parent.name === node) return false;
  if (ts.isVariableDeclaration(parent) && parent.name === node) return false;
  if (ts.isFunctionDeclaration(parent) && parent.name === node) return false;
  if (ts.isClassDeclaration(parent) && parent.name === node) return false;
  if (ts.isInterfaceDeclaration(parent) && parent.name === node) return false;
  if (ts.isTypeAliasDeclaration(parent) && parent.name === node) return false;
  if (ts.isEnumDeclaration(parent) && parent.name === node) return false;
  if (ts.isEnumMember(parent) && parent.name === node) return false;
  if (ts.isBindingElement(parent) && parent.name === node) return false;
  return true;
}

function symbolHasDeclarationInside(symbol: ts.Symbol, container: ts.Node): boolean {
  for (const declaration of symbol.declarations ?? []) {
    if (isDeclarationInsideTarget(declaration, container, container.getSourceFile())) return true;
  }
  return false;
}
