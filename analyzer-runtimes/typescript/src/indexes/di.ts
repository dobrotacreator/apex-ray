import ts from "typescript";

import {
  arrayLiteralExpressionForInitializer,
  identifierFromExpression,
  propertyAssignmentNamed,
  unwrapExpression,
} from "../ast-utils.js";
import { referenceForNode } from "../references/utils.js";
import type { DiInjectionIndexEntry, DiProviderIndexEntry } from "../types.js";
import {
  appendIndexEntry,
  collectionShouldStop,
  type IndexCollectionControl,
} from "./collection.js";

export function collectDiProviderIndex(
  repo: string,
  source: ts.SourceFile,
  control?: IndexCollectionControl,
): DiProviderIndexEntry[] {
  const providers: DiProviderIndexEntry[] = [];
  const providerArrays = collectDiProviderArrays(repo, source, control);
  for (const entries of providerArrays.values()) {
    providers.push(...entries);
  }
  visit(source);
  return providers;

  function visit(node: ts.Node): void {
    if (collectionShouldStop(control)) return;
    if (ts.isObjectLiteralExpression(node)) {
      providers.push(
        ...diProviderEntriesForModuleObject(
          repo,
          source,
          node,
          providerArrays,
          control,
        ),
      );
      providers.push(
        ...diProviderEntriesForObjectLiteral(repo, source, node, control),
      );
    }
    ts.forEachChild(node, visit);
  }
}

function collectDiProviderArrays(
  repo: string,
  source: ts.SourceFile,
  control?: IndexCollectionControl,
): Map<string, DiProviderIndexEntry[]> {
  const providerArrays = new Map<string, DiProviderIndexEntry[]>();
  for (const statement of source.statements) {
    if (collectionShouldStop(control)) break;
    if (!ts.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (collectionShouldStop(control)) break;
      if (!ts.isIdentifier(declaration.name)) continue;
      const arrayName = declaration.name.text;
      const array = arrayLiteralExpressionForInitializer(declaration.initializer);
      if (!array) continue;

      const entries: DiProviderIndexEntry[] = [];
      for (const element of array.elements) {
        if (collectionShouldStop(control)) break;
        const unwrapped = ts.isSpreadElement(element) ? unwrapExpression(element.expression) : unwrapExpression(element);
        if (!unwrapped) continue;
        if (ts.isIdentifier(unwrapped)) {
          if (!appendIndexEntry(entries, {
            tokenName: unwrapped.text,
            implementationName: unwrapped.text,
            reference: referenceForNode(repo, source, unwrapped, "read"),
            sourceArrayName: arrayName,
          }, control)) break;
          continue;
        }
        if (!ts.isObjectLiteralExpression(unwrapped)) continue;
        entries.push(
          ...diProviderEntriesForObjectLiteral(
            repo,
            source,
            unwrapped,
            control,
          ).map((entry) => ({
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
  control?: IndexCollectionControl,
): DiProviderIndexEntry[] {
  const referenceNode = moduleDecoratorForObjectLiteral(object);
  if (!referenceNode) return [];

  const entries: DiProviderIndexEntry[] = [];
  for (const propertyName of ["providers", "exports"]) {
    if (collectionShouldStop(control)) break;
    const property = propertyAssignmentNamed(object, propertyName);
    const array = property ? unwrapExpression(property.initializer) : null;
    if (!array || !ts.isArrayLiteralExpression(array)) continue;

    for (const element of array.elements) {
      if (collectionShouldStop(control)) break;
      if (ts.isSpreadElement(element)) {
        const spreadIdentifier = identifierFromExpression(element.expression);
        const spreadProviders = spreadIdentifier ? providerArrays.get(spreadIdentifier.text) : undefined;
        if (spreadIdentifier && spreadProviders) {
          if (!appendIndexEntry(entries, {
            tokenName: spreadIdentifier.text,
            implementationName: spreadIdentifier.text,
            reference: referenceForNode(repo, source, referenceNode, "read"),
          }, control)) break;
          for (const provider of spreadProviders) {
            if (!appendIndexEntry(entries, {
              tokenName: provider.tokenName,
              implementationName: provider.implementationName,
              reference: referenceForNode(repo, source, referenceNode, "read"),
            }, control)) break;
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
            if (!appendIndexEntry(entries, {
              tokenName: provider.tokenName,
              implementationName: provider.implementationName,
              reference: referenceForNode(repo, source, referenceNode, "read"),
            }, control)) break;
          }
        }
        if (!appendIndexEntry(entries, {
          tokenName: unwrapped.text,
          implementationName: unwrapped.text,
          reference: referenceForNode(repo, source, referenceNode, "read"),
        }, control)) break;
        continue;
      }
      if (ts.isObjectLiteralExpression(unwrapped)) {
        for (const provider of diProviderEntriesForObjectLiteral(
          repo,
          source,
          unwrapped,
          control,
        )) {
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
  control?: IndexCollectionControl,
): DiProviderIndexEntry[] {
  const provideProperty = propertyAssignmentNamed(object, "provide");
  if (!provideProperty) return [];
  const token = identifierFromExpression(provideProperty.initializer);
  if (!token) return [];

  const entries: DiProviderIndexEntry[] = [];
  const appendImplementation = (implementation: ts.Identifier): boolean =>
    appendIndexEntry(entries, {
      tokenName: token.text,
      implementationName: implementation.text,
      reference: referenceForNode(repo, source, implementation, "read"),
    }, control);
  for (const propertyName of ["useClass", "useExisting"]) {
    if (collectionShouldStop(control)) break;
    const property = propertyAssignmentNamed(object, propertyName);
    const implementation = property ? identifierFromExpression(property.initializer) : null;
    if (implementation && !appendImplementation(implementation)) break;
  }

  const injectProperty = propertyAssignmentNamed(object, "inject");
  if (injectProperty) {
    const injectArray = unwrapExpression(injectProperty.initializer);
    if (injectArray && ts.isArrayLiteralExpression(injectArray)) {
      for (const element of injectArray.elements) {
        if (collectionShouldStop(control)) break;
        const implementation = ts.isSpreadElement(element)
          ? identifierFromExpression(element.expression)
          : identifierFromExpression(element);
        if (
          implementation &&
          !appendImplementation(implementation)
        ) {
          break;
        }
      }
    }
  }
  return entries;
}

export function collectDiInjectionIndex(
  repo: string,
  source: ts.SourceFile,
  control?: IndexCollectionControl,
): DiInjectionIndexEntry[] {
  const injections: DiInjectionIndexEntry[] = [];
  visit(source);
  return injections;

  function visit(node: ts.Node): void {
    if (collectionShouldStop(control)) return;
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "Inject") {
      const [argument] = node.arguments;
      const token = identifierFromExpression(argument);
      if (token) {
        appendIndexEntry(injections, {
          tokenName: token.text,
          reference: referenceForNode(repo, source, token, "read"),
        }, control);
      }
    }
    ts.forEachChild(node, visit);
  }
}
