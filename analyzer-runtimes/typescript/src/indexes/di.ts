import ts from "typescript";

import {
  arrayLiteralExpressionForInitializer,
  identifierFromExpression,
  propertyAssignmentNamed,
  unwrapExpression,
} from "../ast-utils.js";
import { REPO_INDEX_SEMANTIC_ENTRY_LIMIT } from "../constants.js";
import { referenceForNode } from "../references/utils.js";
import type { DiInjectionIndexEntry, DiProviderIndexEntry } from "../types.js";
import {
  appendIndexEntry,
  collectionShouldStop,
  type IndexCollectionControl,
} from "./collection.js";

interface DirectConstArrayLiteral {
  array: ts.ArrayLiteralExpression;
  unsafeIdentifierStarts: number[];
}

function visitChildrenUntilStopped(
  node: ts.Node,
  visit: (child: ts.Node) => boolean,
): boolean {
  const stopped = ts.forEachChild(
    node,
    (child) => (visit(child) ? undefined : true),
  );
  return stopped !== true;
}

function createDiAuxiliaryCollectionControl(
  outputControl?: IndexCollectionControl,
): IndexCollectionControl {
  // Prepasses retain lookup state, not semantic output. Keep that state
  // bounded while still inheriting cancellation and deadline checks.
  let reservedEntries = 0;
  let limitReached = false;
  const markLimitReached = (): void => {
    if (limitReached) return;
    limitReached = true;
    outputControl?.markPartial();
  };
  const auxiliaryControl: IndexCollectionControl = {
    shouldStop: () =>
      limitReached || collectionShouldStop(outputControl),
    reserveEntry: () => auxiliaryControl.reserveEntries(1),
    reserveEntries: (count) => {
      if (count < 0 || !Number.isSafeInteger(count)) {
        markLimitReached();
        return false;
      }
      if (auxiliaryControl.shouldStop()) return false;
      if (
        count >
        REPO_INDEX_SEMANTIC_ENTRY_LIMIT - reservedEntries
      ) {
        markLimitReached();
        return false;
      }
      reservedEntries += count;
      return true;
    },
    markPartial: () => outputControl?.markPartial(),
  };
  return auxiliaryControl;
}

export function collectDiProviderIndex(
  repo: string,
  source: ts.SourceFile,
  control?: IndexCollectionControl,
): DiProviderIndexEntry[] {
  const providers: DiProviderIndexEntry[] = [];
  const staticArrays = collectDirectConstArrayLiterals(
    source,
    createDiAuxiliaryCollectionControl(control),
  );
  const providerArrays = collectDiProviderArrays(
    repo,
    source,
    staticArrays,
    createDiAuxiliaryCollectionControl(control),
  );
  // Concrete module/provider objects are higher-signal output than lookup
  // metadata for arrays that may never be referenced.
  if (!visit(source)) return providers;
  for (const entries of providerArrays.values()) {
    for (const entry of entries) {
      if (!appendIndexEntry(providers, entry, control)) {
        return providers;
      }
    }
  }
  return providers;

  function visit(node: ts.Node): boolean {
    if (collectionShouldStop(control)) return false;
    if (ts.isObjectLiteralExpression(node)) {
      providers.push(
        ...diProviderEntriesForModuleObject(
          repo,
          source,
          node,
          providerArrays,
          staticArrays,
          control,
        ),
      );
      providers.push(
        ...diProviderEntriesForObjectLiteral(
          repo,
          source,
          node,
          staticArrays,
          control,
        ),
      );
    }
    return visitChildrenUntilStopped(node, visit);
  }
}

function collectDirectConstArrayLiterals(
  source: ts.SourceFile,
  control: IndexCollectionControl,
): Map<string, DirectConstArrayLiteral> {
  const arrays = new Map<string, DirectConstArrayLiteral>();
  for (const statement of source.statements) {
    if (collectionShouldStop(control)) {
      arrays.clear();
      return arrays;
    }
    if (!ts.isVariableStatement(statement)) continue;
    if (
      (statement.declarationList.flags & ts.NodeFlags.Const) ===
      0
    ) {
      continue;
    }
    for (const declaration of statement.declarationList.declarations) {
      if (!ts.isIdentifier(declaration.name)) continue;
      const array = arrayLiteralExpressionForInitializer(
        declaration.initializer,
      );
      if (array) {
        if (
          !arrays.has(declaration.name.text) &&
          !control.reserveEntry()
        ) {
          arrays.clear();
          return arrays;
        }
        arrays.set(declaration.name.text, {
          array,
          unsafeIdentifierStarts: [],
        });
      }
    }
  }

  if (arrays.size > 0 && !collectUnsafeIdentifierStarts(source)) {
    arrays.clear();
  }
  return arrays;

  function collectUnsafeIdentifierStarts(node: ts.Node): boolean {
    if (collectionShouldStop(control)) return false;
    if (
      ts.isIdentifier(node) &&
      !isReadOnlySpreadIdentifier(node)
    ) {
      const array = arrays.get(node.text);
      if (array) {
        if (!control.reserveEntry()) return false;
        array.unsafeIdentifierStarts.push(node.getStart(source));
      }
    }
    return visitChildrenUntilStopped(
      node,
      collectUnsafeIdentifierStarts,
    );
  }

  function isReadOnlySpreadIdentifier(
    identifier: ts.Identifier,
  ): boolean {
    let expression: ts.Expression = identifier;
    while (true) {
      const parent = expression.parent;
      if (
        (ts.isParenthesizedExpression(parent) ||
          ts.isAsExpression(parent) ||
          ts.isSatisfiesExpression(parent) ||
          ts.isNonNullExpression(parent) ||
          ts.isTypeAssertionExpression(parent)) &&
        parent.expression === expression
      ) {
        expression = parent;
        continue;
      }
      return (
        ts.isSpreadElement(parent) &&
        parent.expression === expression
      );
    }
  }
}

function directConstArrayForSpread(
  source: ts.SourceFile,
  expression: ts.Expression,
  staticArrays: ReadonlyMap<string, DirectConstArrayLiteral>,
): ts.ArrayLiteralExpression | undefined {
  const directArray = arrayLiteralExpressionForInitializer(expression);
  if (directArray) return directArray;

  const identifier = identifierFromExpression(expression);
  if (!identifier) return undefined;
  const staticArray = staticArrays.get(identifier.text);
  const identifierStart = identifier.getStart(source);
  if (
    !staticArray ||
    staticArray.array.end > identifierStart ||
    staticArray.unsafeIdentifierStarts.some(
      (start) =>
        start > staticArray.array.end &&
        start < identifierStart,
    )
  ) {
    return undefined;
  }

  for (
    let ancestor: ts.Node | undefined = identifier.parent;
    ancestor && ancestor !== source;
    ancestor = ancestor.parent
  ) {
    if (
      ts.isFunctionLike(ancestor) ||
      ts.isBlock(ancestor) ||
      ts.isModuleBlock(ancestor) ||
      ts.isCaseBlock(ancestor) ||
      ts.isCatchClause(ancestor) ||
      ts.isForStatement(ancestor) ||
      ts.isForInStatement(ancestor) ||
      ts.isForOfStatement(ancestor) ||
      ts.isWithStatement(ancestor)
    ) {
      return undefined;
    }
  }
  return staticArray.array;
}

function collectDiProviderArrays(
  repo: string,
  source: ts.SourceFile,
  staticArrays: ReadonlyMap<string, DirectConstArrayLiteral>,
  control: IndexCollectionControl,
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
            staticArrays,
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
  staticArrays: ReadonlyMap<string, DirectConstArrayLiteral>,
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
          for (const provider of spreadProviders) {
            if (!appendIndexEntry(entries, {
              tokenName: provider.tokenName,
              implementationName: provider.implementationName,
              reference: referenceForNode(repo, source, referenceNode, "read"),
            }, control)) {
              return entries;
            }
          }
          if (!appendIndexEntry(entries, {
            tokenName: spreadIdentifier.text,
            implementationName: spreadIdentifier.text,
            reference: referenceForNode(repo, source, referenceNode, "read"),
          }, control)) {
            return entries;
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
          staticArrays,
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
  staticArrays: ReadonlyMap<string, DirectConstArrayLiteral>,
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
        if (ts.isSpreadElement(element)) {
          const staticArray = directConstArrayForSpread(
            source,
            element.expression,
            staticArrays,
          );
          if (!staticArray) continue;

          let stopped = false;
          for (const staticElement of staticArray.elements) {
            if (collectionShouldStop(control)) {
              stopped = true;
              break;
            }
            if (ts.isSpreadElement(staticElement)) continue;
            const implementation =
              identifierFromExpression(staticElement);
            if (
              implementation &&
              !appendImplementation(implementation)
            ) {
              stopped = true;
              break;
            }
          }
          if (stopped) break;
          continue;
        }
        const implementation = identifierFromExpression(element);
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

  function visit(node: ts.Node): boolean {
    if (collectionShouldStop(control)) return false;
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "Inject") {
      const [argument] = node.arguments;
      const token = identifierFromExpression(argument);
      if (token) {
        if (!appendIndexEntry(injections, {
          tokenName: token.text,
          reference: referenceForNode(repo, source, token, "read"),
        }, control)) {
          return false;
        }
      }
    }
    return visitChildrenUntilStopped(node, visit);
  }
}
