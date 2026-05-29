import ts from "typescript";

import {
  arrayLiteralExpressionForInitializer,
  identifierFromExpression,
  identifiersFromArrayExpression,
  propertyAssignmentNamed,
  unwrapExpression,
} from "../ast-utils.js";
import { referenceForNode } from "../references/reference-utils.js";
import type { DiInjectionIndexEntry, DiProviderIndexEntry } from "../types.js";

export function collectDiProviderIndex(repo: string, source: ts.SourceFile): DiProviderIndexEntry[] {
  const providers: DiProviderIndexEntry[] = [];
  const providerArrays = collectDiProviderArrays(repo, source);
  for (const entries of providerArrays.values()) {
    providers.push(...entries);
  }
  visit(source);
  return providers;

  function visit(node: ts.Node): void {
    if (ts.isObjectLiteralExpression(node)) {
      providers.push(...diProviderEntriesForModuleObject(repo, source, node, providerArrays));
      providers.push(...diProviderEntriesForObjectLiteral(repo, source, node));
    }
    ts.forEachChild(node, visit);
  }
}

function collectDiProviderArrays(repo: string, source: ts.SourceFile): Map<string, DiProviderIndexEntry[]> {
  const providerArrays = new Map<string, DiProviderIndexEntry[]>();
  for (const statement of source.statements) {
    if (!ts.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (!ts.isIdentifier(declaration.name)) continue;
      const arrayName = declaration.name.text;
      const array = arrayLiteralExpressionForInitializer(declaration.initializer);
      if (!array) continue;

      const entries: DiProviderIndexEntry[] = [];
      for (const element of array.elements) {
        const unwrapped = ts.isSpreadElement(element) ? unwrapExpression(element.expression) : unwrapExpression(element);
        if (!unwrapped) continue;
        if (ts.isIdentifier(unwrapped)) {
          entries.push({
            tokenName: unwrapped.text,
            implementationName: unwrapped.text,
            reference: referenceForNode(repo, source, unwrapped, "read"),
            sourceArrayName: arrayName,
          });
          continue;
        }
        if (!ts.isObjectLiteralExpression(unwrapped)) continue;
        entries.push(
          ...diProviderEntriesForObjectLiteral(repo, source, unwrapped).map((entry) => ({
            ...entry,
            sourceArrayName: arrayName,
          })),
        );
      }
      if (entries.length > 0) {
        providerArrays.set(arrayName, entries);
      }
    }
  }
  return providerArrays;
}

function diProviderEntriesForModuleObject(
  repo: string,
  source: ts.SourceFile,
  object: ts.ObjectLiteralExpression,
  providerArrays: Map<string, DiProviderIndexEntry[]>,
): DiProviderIndexEntry[] {
  const referenceNode = moduleDecoratorForObjectLiteral(object);
  if (!referenceNode) return [];

  const entries: DiProviderIndexEntry[] = [];
  for (const propertyName of ["providers", "exports"]) {
    const property = propertyAssignmentNamed(object, propertyName);
    const array = property ? unwrapExpression(property.initializer) : null;
    if (!array || !ts.isArrayLiteralExpression(array)) continue;

    for (const element of array.elements) {
      if (ts.isSpreadElement(element)) {
        const spreadIdentifier = identifierFromExpression(element.expression);
        const spreadProviders = spreadIdentifier ? providerArrays.get(spreadIdentifier.text) : undefined;
        if (spreadIdentifier && spreadProviders) {
          entries.push({
            tokenName: spreadIdentifier.text,
            implementationName: spreadIdentifier.text,
            reference: referenceForNode(repo, source, referenceNode, "read"),
          });
          for (const provider of spreadProviders) {
            entries.push({
              tokenName: provider.tokenName,
              implementationName: provider.implementationName,
              reference: referenceForNode(repo, source, referenceNode, "read"),
            });
          }
          continue;
        }
      }

      const unwrapped = ts.isSpreadElement(element) ? unwrapExpression(element.expression) : unwrapExpression(element);
      if (!unwrapped) continue;
      if (ts.isIdentifier(unwrapped)) {
        const spreadProviders = providerArrays.get(unwrapped.text);
        if (spreadProviders) {
          for (const provider of spreadProviders) {
            entries.push({
              tokenName: provider.tokenName,
              implementationName: provider.implementationName,
              reference: referenceForNode(repo, source, referenceNode, "read"),
            });
          }
        }
        entries.push({
          tokenName: unwrapped.text,
          implementationName: unwrapped.text,
          reference: referenceForNode(repo, source, referenceNode, "read"),
        });
        continue;
      }
      if (ts.isObjectLiteralExpression(unwrapped)) {
        for (const provider of diProviderEntriesForObjectLiteral(repo, source, unwrapped)) {
          entries.push({
            tokenName: provider.tokenName,
            implementationName: provider.implementationName,
            reference: referenceForNode(repo, source, referenceNode, "read"),
          });
        }
      }
    }
  }
  return entries;
}

function moduleDecoratorForObjectLiteral(object: ts.ObjectLiteralExpression): ts.Decorator | null {
  const call = object.parent;
  if (!ts.isCallExpression(call) || call.arguments[0] !== object) return null;
  if (!ts.isIdentifier(call.expression) || call.expression.text !== "Module") return null;
  return ts.isDecorator(call.parent) ? call.parent : null;
}

function diProviderEntriesForObjectLiteral(
  repo: string,
  source: ts.SourceFile,
  object: ts.ObjectLiteralExpression,
): DiProviderIndexEntry[] {
  const provideProperty = propertyAssignmentNamed(object, "provide");
  if (!provideProperty) return [];
  const token = identifierFromExpression(provideProperty.initializer);
  if (!token) return [];

  const implementations: ts.Identifier[] = [];
  for (const propertyName of ["useClass", "useExisting"]) {
    const property = propertyAssignmentNamed(object, propertyName);
    const implementation = property ? identifierFromExpression(property.initializer) : null;
    if (implementation) {
      implementations.push(implementation);
    }
  }

  const injectProperty = propertyAssignmentNamed(object, "inject");
  if (injectProperty) {
    implementations.push(...identifiersFromArrayExpression(injectProperty.initializer));
  }

  return implementations.map((implementation) => ({
    tokenName: token.text,
    implementationName: implementation.text,
    reference: referenceForNode(repo, source, implementation, "read"),
  }));
}

export function collectDiInjectionIndex(repo: string, source: ts.SourceFile): DiInjectionIndexEntry[] {
  const injections: DiInjectionIndexEntry[] = [];
  visit(source);
  return injections;

  function visit(node: ts.Node): void {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "Inject") {
      const [argument] = node.arguments;
      const token = identifierFromExpression(argument);
      if (token) {
        injections.push({
          tokenName: token.text,
          reference: referenceForNode(repo, source, token, "read"),
        });
      }
    }
    ts.forEachChild(node, visit);
  }
}
