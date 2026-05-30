import ts from "typescript";

import {
  arrayLiteralExpressionForInitializer,
  isObjectFreezeCall,
  propertyNameText,
  unwrapExpression,
} from "../ast-utils.js";
import { canonicalSymbol } from "../checker-utils.js";
import { arrayEntrySymbolName } from "./synthetic-array-entries.js";
import type { CollectedSymbol, DeletedLine } from "../types.js";

export {
  collectConstArrayEntrySymbols,
  collectFactoryCallArrayEntrySymbols,
} from "./synthetic-array-entries.js";

export function collectDeletedSymbols(
  source: ts.SourceFile,
  collectedSymbols: CollectedSymbol[],
  deletedLines: DeletedLine[],
): CollectedSymbol[] {
  const symbols: CollectedSymbol[] = [];
  const seen = new Set<string>();
  for (const deletedLine of deletedLines) {
    const container = deletedContainerForLine(collectedSymbols, deletedLine.line);
    if (!container) continue;

    const name = deletedSymbolNameForLine(container, deletedLine.text);
    if (!name) continue;

    const key = `${container.analysis.name}:${name}:${deletedLine.line}`;
    if (seen.has(key)) continue;
    seen.add(key);

    symbols.push({
      analysis: {
        name,
        kind: ts.isEnumDeclaration(container.node) ? "enum-member" : "variable",
        startLine: deletedLine.line,
        endLine: deletedLine.line,
        exported: container.analysis.exported,
        signature: deletedSymbolSignature(container.analysis.name, name, deletedLine.text),
        references: [],
        callees: [],
        contracts: [],
        metadata: [],
      },
      node: source,
      tsSymbol: container.tsSymbol,
      defaultExported: false,
      exportContainer: container.analysis.exported
        ? { name: container.analysis.name, defaultExported: container.defaultExported }
        : null,
      containerNode: container.node,
    });
  }
  return symbols;
}

export function preferSyntheticChildSymbols(symbols: CollectedSymbol[]): CollectedSymbol[] {
  const containerNodes = new Set(symbols.map((symbol) => symbol.containerNode).filter((node): node is ts.Node => Boolean(node)));
  if (containerNodes.size === 0) return symbols;
  return symbols.filter((symbol) => !containerNodes.has(symbol.node));
}

function deletedContainerForLine(symbols: CollectedSymbol[], line: number): CollectedSymbol | null {
  const candidates = symbols.filter((symbol) => {
    if (!ts.isVariableStatement(symbol.node) && !ts.isEnumDeclaration(symbol.node)) return false;
    return symbol.analysis.startLine <= line && line <= symbol.analysis.endLine + 1;
  });
  return candidates.sort((left, right) => symbolSpan(left) - symbolSpan(right) || right.analysis.startLine - left.analysis.startLine)[0] ?? null;
}

function symbolSpan(symbol: CollectedSymbol): number {
  return symbol.analysis.endLine - symbol.analysis.startLine;
}

function deletedSymbolNameForLine(container: CollectedSymbol, text: string): string | null {
  if (ts.isEnumDeclaration(container.node)) {
    return deletedEnumMemberName(text);
  }
  if (!ts.isVariableStatement(container.node)) {
    return null;
  }

  const declaration = container.node.declarationList.declarations[0];
  if (objectLiteralExpressionForInitializer(declaration?.initializer)) {
    return deletedObjectPropertyName(text);
  }

  if (arrayLiteralExpressionForInitializer(declaration?.initializer)) {
    return deletedArrayEntryName(container.analysis.name, text);
  }
  return null;
}

function deletedEnumMemberName(text: string): string | null {
  const match = /^\s*(?:"([^"]+)"|'([^']+)'|([A-Za-z_$][\w$]*))\s*(?:=|,|$)/.exec(text);
  return match ? match[1] ?? match[2] ?? match[3] : null;
}

function deletedObjectPropertyName(text: string): string | null {
  const source = ts.createSourceFile("__apex_deleted_object.ts", `const __apex = ({\n${text}\n});`, ts.ScriptTarget.Latest, true);
  const statement = source.statements[0];
  if (!statement || !ts.isVariableStatement(statement)) return null;
  const declaration = statement.declarationList.declarations[0];
  const initializer = unwrapExpression(declaration?.initializer);
  if (!initializer || !ts.isObjectLiteralExpression(initializer)) return null;
  const [property] = initializer.properties;
  if (!property || (!ts.isPropertyAssignment(property) && !ts.isShorthandPropertyAssignment(property))) return null;
  return propertyNameText(property.name);
}

function deletedArrayEntryName(containerName: string, text: string): string | null {
  const source = ts.createSourceFile("__apex_deleted_array.ts", `const __apex = [\n${text}\n];`, ts.ScriptTarget.Latest, true);
  const statement = source.statements[0];
  if (!statement || !ts.isVariableStatement(statement)) return null;
  const declaration = statement.declarationList.declarations[0];
  const initializer = unwrapExpression(declaration?.initializer);
  if (!initializer || !ts.isArrayLiteralExpression(initializer)) return null;
  const [element] = initializer.elements;
  if (!element || ts.isSpreadElement(element)) return null;
  return arrayEntrySymbolName(containerName, element, 0, source);
}

function deletedSymbolSignature(containerName: string, name: string, text: string): string {
  const compacted = text.replace(/\s+/g, " ").trim();
  const suffix = compacted.length > 120 ? `${compacted.slice(0, 117)}...` : compacted;
  return `${containerName} removed entry ${name}: ${suffix}`;
}

export function collectEnumMemberSymbols(
  declaration: ts.EnumDeclaration,
  checker: ts.TypeChecker,
  source: ts.SourceFile,
  enumName: string,
  exported: boolean,
  defaultExported: boolean,
): CollectedSymbol[] {
  const symbols: CollectedSymbol[] = [];
  for (const member of declaration.members) {
    const memberName = propertyNameText(member.name);
    if (!memberName) continue;

    const start = source.getLineAndCharacterOfPosition(member.getStart(source)).line + 1;
    const end = source.getLineAndCharacterOfPosition(member.getEnd()).line + 1;
    symbols.push({
      analysis: {
        name: memberName,
        kind: "enum-member",
        startLine: start,
        endLine: end,
        exported,
        signature: enumMemberSignature(enumName, member, source),
        references: [],
        callees: [],
        contracts: [],
        metadata: [],
      },
      node: member,
      tsSymbol: canonicalSymbol(checker, checker.getSymbolAtLocation(member.name)),
      defaultExported: false,
      exportContainer: exported ? { name: enumName, defaultExported } : null,
      containerNode: declaration,
    });
  }
  return symbols;
}

function enumMemberSignature(enumName: string, member: ts.EnumMember, source: ts.SourceFile): string {
  return `${enumName}.${member.getText(source).split("\n")[0].trim()}`;
}

export function collectConstObjectMemberSymbols(
  statement: ts.VariableStatement,
  checker: ts.TypeChecker,
  source: ts.SourceFile,
  containerName: string,
  defaultExported: boolean,
): CollectedSymbol[] {
  const declaration = statement.declarationList.declarations[0];
  const initializer = objectLiteralExpressionForInitializer(declaration?.initializer);
  if (!initializer) return [];

  const symbols: CollectedSymbol[] = [];
  for (const property of initializer.properties) {
    if (!ts.isPropertyAssignment(property) && !ts.isShorthandPropertyAssignment(property)) continue;
    const propertyName = propertyNameText(property.name);
    if (!propertyName) continue;

    const start = source.getLineAndCharacterOfPosition(property.getStart(source)).line + 1;
    const end = source.getLineAndCharacterOfPosition(property.getEnd()).line + 1;
    symbols.push({
      analysis: {
        name: propertyName,
        kind: "variable",
        startLine: start,
        endLine: end,
        exported: true,
        signature: syntheticNodeSignature(property, source),
        references: [],
        callees: [],
        contracts: [],
        metadata: [],
      },
      node: property,
      tsSymbol: canonicalSymbol(checker, checker.getSymbolAtLocation(property.name)),
      defaultExported: false,
      exportContainer: { name: containerName, defaultExported },
      containerNode: statement,
    });
  }
  return symbols;
}

function objectLiteralExpressionForInitializer(expression: ts.Expression | undefined): ts.ObjectLiteralExpression | null {
  const unwrapped = unwrapExpression(expression);
  if (!unwrapped) return null;
  if (ts.isObjectLiteralExpression(unwrapped)) return unwrapped;
  if (!isObjectFreezeCall(unwrapped)) return null;

  const [argument] = unwrapped.arguments;
  const objectArgument = unwrapExpression(argument);
  return objectArgument && ts.isObjectLiteralExpression(objectArgument) ? objectArgument : null;
}

function syntheticNodeSignature(node: ts.Node, source: ts.SourceFile): string {
  const text = node.getText(source).split("\n")[0].trim();
  return text.length > 200 ? `${text.slice(0, 197)}...` : text;
}
