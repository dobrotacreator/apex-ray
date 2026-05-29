import ts from "typescript";

import { decoratorsForNode } from "../ast-utils.js";
import { canonicalSymbol } from "../checker-utils.js";
import { collectExportedSymbolInfo } from "./export-info.js";
import {
  collectConstArrayEntrySymbols,
  collectConstObjectMemberSymbols,
  collectEnumMemberSymbols,
  collectFactoryCallArrayEntrySymbols,
} from "./synthetic.js";
import type {
  CollectedSymbol,
  ExportContainer,
  ExportedSymbolInfo,
  SymbolKind,
} from "../types.js";

export { collectExports, collectImports } from "./export-info.js";
export { collectDeletedSymbols, preferSyntheticChildSymbols } from "./synthetic.js";

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

function nodeStartIncludingDecorators(source: ts.SourceFile, node: ts.Node): number {
  let start = node.getStart(source);
  for (const decorator of decoratorsForNode(node)) {
    start = Math.min(start, decorator.getStart(source));
  }
  return start;
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
