import ts from "typescript";

import {
  arrayLiteralExpressionForInitializer,
  decoratorsForNode,
  isObjectFreezeCall,
  moduleExportNameText,
  propertyAssignmentNamed,
  propertyNameText,
  unwrapExpression,
} from "./ast-utils.js";
import { canonicalSymbol } from "./checker-utils.js";
import { ARRAY_OBJECT_ENTRY_ID_PROPERTY_NAMES } from "./constants.js";
import { commonJsExportEntries } from "./repo-index.js";
import type {
  CollectedSymbol,
  DeletedLine,
  ExportContainer,
  ExportedSymbolInfo,
  SymbolKind,
} from "./types.js";

export function collectSymbols(source: ts.SourceFile, checker: ts.TypeChecker): CollectedSymbol[] {
  const symbols: CollectedSymbol[] = [];
  const exportInfo = collectExportedSymbolInfo(source);

  function visit(node: ts.Node): void {
    const kind = symbolKind(node);
    const name = symbolName(node);
    if (name && kind !== "unknown") {
      const start = source.getLineAndCharacterOfPosition(nodeStartIncludingDecorators(source, node)).line + 1;
      const end = source.getLineAndCharacterOfPosition(node.getEnd()).line + 1;
      const defaultExported = isDefaultExportedNode(node) || exportInfo.defaultNames.has(name);
      const exported = isExported(node) || exportInfo.named.has(name) || defaultExported;
      const exportContainer = exportContainerForNode(node, exportInfo);
      const tsSymbol = symbolForNode(node, checker);
      symbols.push({
        analysis: {
          name,
          kind,
          startLine: start,
          endLine: end,
          exported,
          signature: signatureFor(node, checker, source),
          references: [],
          callees: [],
          contracts: [],
          metadata: [],
        },
        node,
        tsSymbol,
        defaultExported,
        exportContainer,
      });

      if (ts.isVariableStatement(node)) {
        if (exported) {
          symbols.push(...collectConstObjectMemberSymbols(node, checker, source, name, defaultExported));
        }
        symbols.push(...collectConstArrayEntrySymbols(node, source, name, exported, defaultExported, tsSymbol));
        symbols.push(...collectFactoryCallArrayEntrySymbols(node, source, name, exported, defaultExported, tsSymbol));
      } else if (ts.isEnumDeclaration(node)) {
        symbols.push(...collectEnumMemberSymbols(node, checker, source, name, exported, defaultExported));
      }
    }
    ts.forEachChild(node, visit);
  }

  visit(source);
  return symbols;
}

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

export function collectImports(source: ts.SourceFile): string[] {
  const imports: string[] = [];
  for (const statement of source.statements) {
    if (ts.isImportDeclaration(statement)) {
      imports.push(statement.getText(source));
    }
  }
  return imports;
}

export function collectExports(source: ts.SourceFile): string[] {
  const exports: string[] = [];
  for (const statement of source.statements) {
    if (ts.isExpressionStatement(statement) && commonJsExportEntries(statement.expression).length > 0) {
      exports.push(statement.getText(source));
      continue;
    }
    if (ts.isExportDeclaration(statement) || ts.isExportAssignment(statement)) {
      exports.push(statement.getText(source));
      continue;
    }
    if (ts.canHaveModifiers(statement) && ts.getModifiers(statement)?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword)) {
      exports.push(statement.getText(source).split("\n")[0].trim());
    }
  }
  return exports;
}

function nodeStartIncludingDecorators(source: ts.SourceFile, node: ts.Node): number {
  let start = node.getStart(source);
  for (const decorator of decoratorsForNode(node)) {
    start = Math.min(start, decorator.getStart(source));
  }
  return start;
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

function collectEnumMemberSymbols(
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

function collectConstObjectMemberSymbols(
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
        signature: signatureFor(property, checker, source),
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

function collectConstArrayEntrySymbols(
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

function collectFactoryCallArrayEntrySymbols(
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

function isConstVariableStatement(statement: ts.VariableStatement): boolean {
  return (statement.declarationList.flags & ts.NodeFlags.Const) !== 0;
}

function arrayEntrySymbolName(
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

function collectExportedSymbolInfo(source: ts.SourceFile): ExportedSymbolInfo {
  const named = new Set<string>();
  const defaultNames = new Set<string>();

  for (const statement of source.statements) {
    if (ts.isExpressionStatement(statement)) {
      for (const entry of commonJsExportEntries(statement.expression)) {
        if (entry.defaultExported) {
          defaultNames.add(entry.localName);
        } else {
          named.add(entry.localName);
        }
      }
      continue;
    }

    if (ts.isExportDeclaration(statement)) {
      if (statement.moduleSpecifier || !statement.exportClause || !ts.isNamedExports(statement.exportClause)) {
        continue;
      }
      for (const specifier of statement.exportClause.elements) {
        const localName = moduleExportNameText(specifier.propertyName ?? specifier.name);
        const exportedName = moduleExportNameText(specifier.name);
        if (exportedName === "default") {
          defaultNames.add(localName);
        } else {
          named.add(localName);
        }
      }
      continue;
    }

    if (ts.isExportAssignment(statement) && ts.isIdentifier(statement.expression)) {
      defaultNames.add(statement.expression.text);
    }
  }

  return { named, defaultNames };
}

function exportContainerForNode(node: ts.Node, exportInfo: ExportedSymbolInfo): ExportContainer | null {
  if (!ts.isMethodDeclaration(node)) return null;
  const parent = node.parent;
  if (!ts.isClassDeclaration(parent) || !parent.name) return null;

  const name = parent.name.text;
  const defaultExported = isDefaultExportedNode(parent) || exportInfo.defaultNames.has(name);
  const exported = isExported(parent) || exportInfo.named.has(name) || defaultExported;
  return exported ? { name, defaultExported } : null;
}

function symbolKind(node: ts.Node): SymbolKind {
  if (ts.isFunctionDeclaration(node) || ts.isArrowFunction(node) || ts.isFunctionExpression(node)) return "function";
  if (ts.isClassDeclaration(node)) return "class";
  if (ts.isEnumDeclaration(node)) return "enum";
  if (ts.isMethodDeclaration(node)) return "method";
  if (ts.isInterfaceDeclaration(node)) return "interface";
  if (ts.isTypeAliasDeclaration(node)) return "type";
  if (ts.isVariableStatement(node)) return "variable";
  return "unknown";
}

function symbolName(node: ts.Node): string | null {
  if ((ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node)) && !node.name && isDefaultExportedNode(node)) {
    return "default";
  }
  if (
    (ts.isFunctionDeclaration(node) ||
      ts.isClassDeclaration(node) ||
      ts.isEnumDeclaration(node) ||
      ts.isInterfaceDeclaration(node) ||
      ts.isTypeAliasDeclaration(node) ||
      ts.isMethodDeclaration(node)) &&
    node.name
  ) {
    return node.name.getText();
  }
  if (ts.isVariableStatement(node)) {
    const declaration = node.declarationList.declarations[0];
    if (declaration?.name) return declaration.name.getText();
  }
  if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) {
    const parent = node.parent;
    if (ts.isVariableDeclaration(parent) && parent.name) return parent.name.getText();
    if (ts.isPropertyAssignment(parent)) return parent.name.getText();
  }
  return null;
}

function symbolNameNode(node: ts.Node): ts.Node | null {
  if (
    (ts.isFunctionDeclaration(node) ||
      ts.isClassDeclaration(node) ||
      ts.isEnumDeclaration(node) ||
      ts.isInterfaceDeclaration(node) ||
      ts.isTypeAliasDeclaration(node) ||
      ts.isMethodDeclaration(node)) &&
    node.name
  ) {
    return node.name;
  }
  if (ts.isVariableStatement(node)) {
    return node.declarationList.declarations[0]?.name ?? null;
  }
  if (ts.isArrowFunction(node) || ts.isFunctionExpression(node)) {
    const parent = node.parent;
    if (ts.isVariableDeclaration(parent) && parent.name) return parent.name;
    if (ts.isPropertyAssignment(parent)) return parent.name;
  }
  return null;
}

function symbolForNode(node: ts.Node, checker: ts.TypeChecker): ts.Symbol | null {
  const nameNode = symbolNameNode(node);
  if (!nameNode) return null;
  return canonicalSymbol(checker, checker.getSymbolAtLocation(nameNode));
}

function isExported(node: ts.Node): boolean {
  return ts.canHaveModifiers(node) && Boolean(ts.getModifiers(node)?.some((modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword));
}

function isDefaultExportedNode(node: ts.Node): boolean {
  return ts.canHaveModifiers(node) && Boolean(ts.getModifiers(node)?.some((modifier) => modifier.kind === ts.SyntaxKind.DefaultKeyword));
}

function signatureFor(node: ts.Node, checker: ts.TypeChecker, source: ts.SourceFile): string {
  if (
    ts.isFunctionDeclaration(node) ||
    ts.isMethodDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isArrowFunction(node)
  ) {
    const signature = checker.getSignatureFromDeclaration(node);
    if (signature) return checker.signatureToString(signature);
  }
  const text = node.getText(source).split("\n")[0].trim();
  return text.length > 200 ? `${text.slice(0, 197)}...` : text;
}
