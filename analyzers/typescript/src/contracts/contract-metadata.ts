import ts from "typescript";

import { calleeNameNode, decoratorsForNode, unwrapExpression } from "../ast-utils.js";
import { canonicalSymbol } from "../checker-utils.js";
import { REFLECTOR_METADATA_METHOD_NAMES } from "../constants.js";
import { isDeclarationInsideTarget } from "../declaration-utils.js";
import { addReference } from "../references/reference-merge.js";
import { referenceForNode } from "../references/reference-utils.js";
import { metadataNodesForTarget } from "./contract-targets.js";
import type { CollectedSymbol, MetadataKeyIdentity, Reference } from "../types.js";
import { isInsideRepo } from "../utils.js";

export function collectDecoratorMetadataKeyConsumerContracts(
  refs: Reference[],
  seen: Set<string>,
  program: ts.Program,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): void {
  const keys = metadataKeysForTargetDecorators(checker, target);
  if (keys.length === 0) return;

  for (const source of program.getSourceFiles()) {
    if (refs.length >= limit) return;
    if (source.isDeclarationFile || !isInsideRepo(repo, source.fileName)) continue;
    visit(source);
  }

  function visit(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (ts.isCallExpression(node) && isReflectorMetadataConsumerCall(node)) {
      const keyArgument = node.arguments[0];
      if (keyArgument && metadataKeyMatches(checker, keyArgument, keys)) {
        const declaration = enclosingMetadataConsumerDeclaration(node);
        if (declaration && !isDeclarationInsideTarget(declaration, target.node, target.node.getSourceFile())) {
          addReference(refs, seen, referenceForNode(repo, declaration.getSourceFile(), declaration, "contract"), limit);
        }
      }
    }
    ts.forEachChild(node, visit);
  }
}

function metadataKeysForTargetDecorators(checker: ts.TypeChecker, target: CollectedSymbol): MetadataKeyIdentity[] {
  const keys: MetadataKeyIdentity[] = [];
  const seen = new Set<string>();
  for (const node of metadataNodesForTarget(target)) {
    for (const decorator of decoratorsForNode(node)) {
      const decoratorName = decoratorNameNode(decorator);
      const decoratorSymbol = decoratorName ? canonicalSymbol(checker, checker.getSymbolAtLocation(decoratorName)) : null;
      if (!decoratorSymbol) continue;
      for (const declaration of decoratorSymbol.declarations ?? []) {
        collectMetadataProducerKeysFromDeclaration(keys, seen, checker, declaration);
      }
    }
  }
  return keys;
}

function collectMetadataProducerKeysFromDeclaration(
  keys: MetadataKeyIdentity[],
  seen: Set<string>,
  checker: ts.TypeChecker,
  declaration: ts.Declaration,
): void {
  visit(declaration);

  function visit(node: ts.Node): void {
    if (ts.isCallExpression(node) && isSetMetadataCall(node)) {
      const key = metadataKeyIdentityForExpression(checker, node.arguments[0]);
      addMetadataKeyIdentity(keys, seen, key);
    }
    ts.forEachChild(node, visit);
  }
}

function isSetMetadataCall(node: ts.CallExpression): boolean {
  const callee = calleeNameNode(node.expression);
  return Boolean(callee && ts.isIdentifier(callee) && callee.text === "SetMetadata");
}

function addMetadataKeyIdentity(keys: MetadataKeyIdentity[], seen: Set<string>, key: MetadataKeyIdentity | null): void {
  if (!key) return;
  const identityKey = key.symbol
    ? `symbol:${key.symbol.name}:${key.symbol.declarations?.[0]?.getSourceFile().fileName ?? ""}`
    : `text:${key.text}`;
  if (seen.has(identityKey)) return;
  seen.add(identityKey);
  keys.push(key);
}

function metadataKeyMatches(
  checker: ts.TypeChecker,
  expression: ts.Expression,
  keys: MetadataKeyIdentity[],
): boolean {
  const candidate = metadataKeyIdentityForExpression(checker, expression);
  if (!candidate) return false;
  return keys.some((key) => metadataKeyIdentitiesMatch(key, candidate));
}

function metadataKeyIdentitiesMatch(left: MetadataKeyIdentity, right: MetadataKeyIdentity): boolean {
  if (left.symbol && right.symbol && left.symbol === right.symbol) return true;
  if (left.text && right.text && left.text === right.text) return true;
  return false;
}

function metadataKeyIdentityForExpression(
  checker: ts.TypeChecker,
  expression: ts.Expression | undefined,
): MetadataKeyIdentity | null {
  const unwrapped = unwrapExpression(expression);
  if (!unwrapped) return null;
  const symbolNode = metadataKeySymbolNode(unwrapped);
  const symbol = symbolNode ? canonicalSymbol(checker, checker.getSymbolAtLocation(symbolNode)) : null;
  const text = metadataKeyComparableText(unwrapped, symbolNode);
  if (!symbol && !text) return null;
  return { symbol, text };
}

function metadataKeySymbolNode(expression: ts.Expression): ts.Node | null {
  if (ts.isIdentifier(expression)) return expression;
  if (ts.isPropertyAccessExpression(expression)) return expression.name;
  if (ts.isElementAccessExpression(expression) && expression.argumentExpression) {
    return expression.argumentExpression;
  }
  return null;
}

function metadataKeyComparableText(expression: ts.Expression, symbolNode: ts.Node | null): string | null {
  if (ts.isStringLiteralLike(expression)) return expression.text;
  if (ts.isNumericLiteral(expression)) return expression.text;
  return symbolNode?.getText() ?? expression.getText();
}

function isReflectorMetadataConsumerCall(node: ts.CallExpression): boolean {
  const expression = node.expression;
  if (!ts.isPropertyAccessExpression(expression)) return false;
  const methodName = expression.name.text;
  if (!REFLECTOR_METADATA_METHOD_NAMES.has(methodName)) return false;
  return methodName !== "get" || isReflectorLikeReceiver(expression.expression);
}

function isReflectorLikeReceiver(expression: ts.Expression): boolean {
  if (ts.isIdentifier(expression)) return expression.text.toLowerCase().includes("reflector");
  if (ts.isPropertyAccessExpression(expression)) {
    return expression.name.text.toLowerCase().includes("reflector") || isReflectorLikeReceiver(expression.expression);
  }
  return false;
}

function enclosingMetadataConsumerDeclaration(node: ts.Node): ts.Declaration | null {
  let current: ts.Node | undefined = node;
  let fallback: ts.Declaration | null = null;
  while (current) {
    if (ts.isClassDeclaration(current) && current.name) return current;
    if (
      !fallback &&
      (ts.isMethodDeclaration(current) ||
        ts.isFunctionDeclaration(current) ||
        ts.isVariableDeclaration(current))
    ) {
      fallback = current;
    }
    current = current.parent;
  }
  return fallback;
}

export function decoratorNameNode(decorator: ts.Decorator): ts.Node | null {
  const expression = decorator.expression;
  if (ts.isCallExpression(expression)) return calleeNameNode(expression.expression);
  if (ts.isIdentifier(expression)) return expression;
  if (ts.isPropertyAccessExpression(expression)) return expression.name;
  return null;
}

export function decoratorArgumentExpressions(decorator: ts.Decorator): readonly ts.Expression[] {
  const expression = decorator.expression;
  return ts.isCallExpression(expression) ? expression.arguments : [];
}

export function collectFrameworkMetadata(target: CollectedSymbol, repo: string, limit: number): Reference[] {
  const refs: Reference[] = [];
  const seen = new Set<string>();
  for (const node of metadataNodesForTarget(target)) {
    for (const decorator of decoratorsForNode(node)) {
      addReference(refs, seen, referenceForNode(repo, decorator.getSourceFile(), decorator, "metadata"), limit);
      if (refs.length >= limit) return refs;
    }
  }
  return refs;
}
