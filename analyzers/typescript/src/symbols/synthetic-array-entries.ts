import ts from "typescript";

import {
  arrayLiteralExpressionForInitializer,
  propertyAssignmentNamed,
  unwrapExpression,
} from "../ast-utils.js";
import { ARRAY_OBJECT_ENTRY_ID_PROPERTY_NAMES } from "../constants.js";
import type { CollectedSymbol } from "../types.js";

export function collectConstArrayEntrySymbols(
  statement: ts.VariableStatement,
  source: ts.SourceFile,
  containerName: string,
  exported: boolean,
  defaultExported: boolean,
  containerSymbol: ts.Symbol | null,
): CollectedSymbol[] {
  if (!isConstVariableStatement(statement)) return [];
  const declaration = statement.declarationList.declarations[0];
  const initializer = arrayLiteralExpressionForInitializer(declaration?.initializer);
  if (!initializer || !ts.isArrayLiteralExpression(initializer)) return [];

  const symbols: CollectedSymbol[] = [];
  initializer.elements.forEach((element, index) => {
    if (ts.isSpreadElement(element)) return;
    const entry = unwrapExpression(element);
    if (!entry) return;

    const name = arrayEntrySymbolName(containerName, entry, index, source);
    if (!name) return;

    const start = source.getLineAndCharacterOfPosition(entry.getStart(source)).line + 1;
    const end = source.getLineAndCharacterOfPosition(entry.getEnd()).line + 1;
    symbols.push({
      analysis: {
        name,
        kind: "variable",
        startLine: start,
        endLine: end,
        exported,
        signature: arrayEntrySignature(containerName, entry, index, source),
        references: [],
        callees: [],
        contracts: [],
        metadata: [],
      },
      node: entry,
      tsSymbol: containerSymbol,
      defaultExported: false,
      exportContainer: exported ? { name: containerName, defaultExported } : null,
      containerNode: statement,
    });
  });
  return symbols;
}

export function collectFactoryCallArrayEntrySymbols(
  statement: ts.VariableStatement,
  source: ts.SourceFile,
  containerName: string,
  exported: boolean,
  defaultExported: boolean,
  containerSymbol: ts.Symbol | null,
): CollectedSymbol[] {
  if (!isConstVariableStatement(statement)) return [];
  const declaration = statement.declarationList.declarations[0];
  const initializer = unwrapExpression(declaration?.initializer);
  if (!initializer || !ts.isCallExpression(initializer)) return [];

  const symbols: CollectedSymbol[] = [];
  for (const argument of initializer.arguments) {
    const array = unwrapExpression(argument);
    if (!array || !ts.isArrayLiteralExpression(array)) continue;
    array.elements.forEach((element, index) => {
      if (ts.isSpreadElement(element)) return;
      const entry = unwrapExpression(element);
      if (!entry) return;

      const name = arrayEntrySymbolName(containerName, entry, index, source);
      if (!name) return;

      const start = source.getLineAndCharacterOfPosition(entry.getStart(source)).line + 1;
      const end = source.getLineAndCharacterOfPosition(entry.getEnd()).line + 1;
      symbols.push({
        analysis: {
          name,
          kind: "variable",
          startLine: start,
          endLine: end,
          exported,
          signature: arrayEntrySignature(containerName, entry, index, source),
          references: [],
          callees: [],
          contracts: [],
          metadata: [],
        },
        node: entry,
        tsSymbol: containerSymbol,
        defaultExported: false,
        exportContainer: exported ? { name: containerName, defaultExported } : null,
        containerNode: statement,
      });
    });
  }
  return symbols;
}

export function arrayEntrySymbolName(
  containerName: string,
  entry: ts.Expression,
  index: number,
  source: ts.SourceFile,
): string | null {
  if (ts.isArrayLiteralExpression(entry)) {
    const tupleKey = tupleEntryKeyText(entry, source);
    return tupleKey ? `${containerName}:${compactSymbolNameSegment(tupleKey)}` : null;
  }

  if (!ts.isObjectLiteralExpression(entry)) {
    const value = compactExpressionValueText(entry, source);
    return value ? `${containerName}:${compactSymbolNameSegment(value)}` : null;
  }

  const method = literalPropertyValueText(entry, "method", source);
  const template = literalPropertyValueText(entry, "template", source);
  if (method && template) {
    return `${containerName}:${compactSymbolNameSegment(`${method} ${template}`)}`;
  }

  for (const propertyName of ARRAY_OBJECT_ENTRY_ID_PROPERTY_NAMES) {
    const value = literalPropertyValueText(entry, propertyName, source);
    if (value) {
      return `${containerName}:${compactSymbolNameSegment(value)}`;
    }
  }

  return `${containerName}:entry-${index + 1}`;
}

function arrayEntrySignature(
  containerName: string,
  entry: ts.Expression,
  index: number,
  source: ts.SourceFile,
): string {
  if (ts.isArrayLiteralExpression(entry)) {
    const tupleKey = tupleEntryKeyText(entry, source);
    return tupleKey ? `${containerName} entry ${tupleKey}` : `${containerName} entry ${index + 1}`;
  }

  if (!ts.isObjectLiteralExpression(entry)) {
    const value = compactExpressionValueText(entry, source);
    return value ? `${containerName} entry ${value}` : `${containerName} entry ${index + 1}`;
  }

  const method = literalPropertyValueText(entry, "method", source);
  const template = literalPropertyValueText(entry, "template", source);
  if (method && template) return `${containerName} entry ${method} ${template}`;

  for (const propertyName of ARRAY_OBJECT_ENTRY_ID_PROPERTY_NAMES) {
    const value = literalPropertyValueText(entry, propertyName, source);
    if (value) return `${containerName} entry ${propertyName}=${value}`;
  }

  return `${containerName} entry ${index + 1}`;
}

function isConstVariableStatement(statement: ts.VariableStatement): boolean {
  return (statement.declarationList.flags & ts.NodeFlags.Const) !== 0;
}

function tupleEntryKeyText(entry: ts.ArrayLiteralExpression, source: ts.SourceFile): string | null {
  const [key] = entry.elements;
  if (!key || ts.isSpreadElement(key)) return null;
  const first = compactExpressionValueText(key, source);
  if (!first) return null;
  const second = entry.elements[1];
  if (second && !ts.isSpreadElement(second)) {
    const secondValue = compactExpressionValueText(second, source);
    if (secondValue) return `${first} ${secondValue}`;
  }
  return first;
}

function literalPropertyValueText(
  entry: ts.ObjectLiteralExpression,
  propertyName: string,
  source: ts.SourceFile,
): string | null {
  const property = propertyAssignmentNamed(entry, propertyName);
  return property ? compactExpressionValueText(property.initializer, source) : null;
}

function compactExpressionValueText(expression: ts.Expression, source: ts.SourceFile): string | null {
  const unwrapped = unwrapExpression(expression);
  if (!unwrapped) return null;
  if (ts.isStringLiteral(unwrapped) || ts.isNoSubstitutionTemplateLiteral(unwrapped) || ts.isNumericLiteral(unwrapped)) {
    return unwrapped.text;
  }
  if (unwrapped.kind === ts.SyntaxKind.TrueKeyword) return "true";
  if (unwrapped.kind === ts.SyntaxKind.FalseKeyword) return "false";
  if (ts.isIdentifier(unwrapped) || ts.isPropertyAccessExpression(unwrapped)) {
    return unwrapped.getText(source);
  }
  return null;
}

function compactSymbolNameSegment(value: string): string {
  const compacted = value.replace(/\s+/g, " ").trim();
  return compacted.length > 100 ? `${compacted.slice(0, 97)}...` : compacted;
}
