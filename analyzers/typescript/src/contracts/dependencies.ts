import path from "node:path";

import ts from "typescript";

import { entityNameText, propertyNameText } from "../ast-utils.js";
import { canonicalSymbol } from "../checker-utils.js";
import { CONTRACT_DEPENDENCY_DEPTH_LIMIT, IGNORED_CONTRACT_DEPENDENCY_NAMES } from "../constants.js";
import { isDeclarationInsideTarget } from "../declaration-utils.js";
import { addReference } from "../references/merge.js";
import { hasAncestor, referenceForNode } from "../references/utils.js";
import type { Reference } from "../types.js";
import { isInsideRepo, isRepoRelativePath, normalizeRelPath } from "../utils.js";

export function addContractDeclarationReferences(
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

export function addContractSymbolWithDependencies(
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

export function contractDeclarationsForSymbol(symbol: ts.Symbol, targetNode: ts.Node, repo: string): ts.Declaration[] {
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

export function isContractDependencyIdentifier(node: ts.Identifier): boolean {
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

function symbolHasDeclarationInside(symbol: ts.Symbol, container: ts.Node): boolean {
  for (const declaration of symbol.declarations ?? []) {
    if (isDeclarationInsideTarget(declaration, container, container.getSourceFile())) return true;
  }
  return false;
}
