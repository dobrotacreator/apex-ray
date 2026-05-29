import ts from "typescript";

import { isObjectFreezeCall } from "./ast-utils.js";
import type { CollectedSymbol } from "./types.js";

export function parametersForNode(node: ts.Node): readonly ts.ParameterDeclaration[] {
  if (ts.isClassDeclaration(node)) {
    return constructorParametersForClass(node);
  }
  if (
    ts.isMethodDeclaration(node) ||
    ts.isFunctionDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isArrowFunction(node) ||
    ts.isConstructorDeclaration(node)
  ) {
    return node.parameters;
  }
  return [];
}

export function constructorParametersForClass(node: ts.ClassDeclaration): readonly ts.ParameterDeclaration[] {
  const constructor = node.members.find((member): member is ts.ConstructorDeclaration => ts.isConstructorDeclaration(member));
  return constructor?.parameters ?? [];
}

export function returnTypeForNode(node: ts.Node): ts.TypeNode | null {
  if (
    ts.isMethodDeclaration(node) ||
    ts.isFunctionDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isArrowFunction(node)
  ) {
    return node.type ?? null;
  }
  return null;
}

export function variableTypeNodesForTarget(target: CollectedSymbol): ts.TypeNode[] {
  const declaration = variableDeclarationForNode(target.node) ??
    (target.containerNode ? variableDeclarationForNode(target.containerNode) : null);
  if (!declaration) return [];

  const typeNodes: ts.TypeNode[] = [];
  if (declaration.type) {
    typeNodes.push(declaration.type);
  }
  typeNodes.push(...expressionTypeContextNodes(declaration.initializer));
  return typeNodes;
}

function variableDeclarationForNode(node: ts.Node): ts.VariableDeclaration | null {
  let current: ts.Node | undefined = node;
  while (current) {
    if (ts.isVariableDeclaration(current)) return current;
    if (ts.isVariableStatement(current)) return current.declarationList.declarations[0] ?? null;
    if (ts.isSourceFile(current)) return null;
    current = current.parent;
  }
  return null;
}

function expressionTypeContextNodes(expression: ts.Expression | undefined): ts.TypeNode[] {
  if (!expression) return [];
  const typeNodes: ts.TypeNode[] = [];
  visit(expression);
  return typeNodes;

  function visit(node: ts.Expression): void {
    let current = node;
    while (true) {
      if (ts.isParenthesizedExpression(current) || ts.isNonNullExpression(current)) {
        current = current.expression;
        continue;
      }
      if (ts.isAsExpression(current) || ts.isTypeAssertionExpression(current)) {
        typeNodes.push(current.type);
        current = current.expression;
        continue;
      }
      if (ts.isSatisfiesExpression(current)) {
        typeNodes.push(current.type);
        current = current.expression;
        continue;
      }
      break;
    }

    if (isObjectFreezeCall(current)) {
      for (const argument of current.arguments) {
        visit(argument);
      }
    }
  }
}

export function metadataNodesForTarget(target: CollectedSymbol): ts.Node[] {
  const nodes: ts.Node[] = [];
  if (ts.isMethodDeclaration(target.node)) {
    const parent = target.node.parent;
    if (ts.isClassDeclaration(parent)) {
      nodes.push(parent);
    }
    nodes.push(target.node);
    nodes.push(...target.node.parameters);
  } else if (ts.isClassDeclaration(target.node)) {
    nodes.push(target.node);
    nodes.push(...constructorParametersForClass(target.node));
    for (const member of target.node.members) {
      nodes.push(member);
      if (ts.isMethodDeclaration(member) || ts.isConstructorDeclaration(member)) {
        nodes.push(...member.parameters);
      }
    }
  }
  return nodes;
}
