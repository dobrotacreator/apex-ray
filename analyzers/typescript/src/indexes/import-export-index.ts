import ts from "typescript";

import { moduleExportNameText, propertyNameText } from "../ast-utils.js";
import { NAMESPACE_EXPORT_LOCAL_NAME, STAR_EXPORT_LOCAL_NAME } from "../constants.js";
import { referenceForNode } from "../references/reference-utils.js";
import type { CommonJsExportEntry, ExportIndexEntry } from "../types.js";

export { collectImportIndex } from "./import-index.js";

export function collectExportIndex(repo: string, source: ts.SourceFile): ExportIndexEntry[] {
  const exports: ExportIndexEntry[] = [];
  for (const statement of source.statements) {
    if (ts.isExpressionStatement(statement)) {
      for (const entry of commonJsExportEntries(statement.expression)) {
        exports.push({
          moduleSpecifier: null,
          localName: entry.localName,
          exportedName: entry.exportedName,
          reference: referenceForNode(repo, source, entry.referenceNode, "import"),
        });
      }
      continue;
    }
    if (!ts.isExportDeclaration(statement)) continue;

    const moduleSpecifierNode = statement.moduleSpecifier;
    const moduleSpecifier = moduleSpecifierNode && ts.isStringLiteral(moduleSpecifierNode)
      ? moduleSpecifierNode.text
      : null;
    if (!statement.exportClause) {
      if (moduleSpecifier === null || !moduleSpecifierNode) continue;
      exports.push({
        moduleSpecifier,
        localName: STAR_EXPORT_LOCAL_NAME,
        exportedName: STAR_EXPORT_LOCAL_NAME,
        reference: referenceForNode(repo, source, moduleSpecifierNode, "import"),
      });
      continue;
    }
    if (ts.isNamespaceExport(statement.exportClause)) {
      if (moduleSpecifier === null) continue;
      exports.push({
        moduleSpecifier,
        localName: NAMESPACE_EXPORT_LOCAL_NAME,
        exportedName: statement.exportClause.name.text,
        reference: referenceForNode(repo, source, statement.exportClause.name, "import"),
      });
      continue;
    }

    if (!ts.isNamedExports(statement.exportClause)) continue;
    for (const specifier of statement.exportClause.elements) {
      exports.push({
        moduleSpecifier,
        localName: moduleExportNameText(specifier.propertyName ?? specifier.name),
        exportedName: moduleExportNameText(specifier.name),
        reference: referenceForNode(repo, source, specifier.name, "import"),
      });
    }
  }
  return exports;
}

export function commonJsExportEntries(expression: ts.Expression): CommonJsExportEntry[] {
  if (!ts.isBinaryExpression(expression) || expression.operatorToken.kind !== ts.SyntaxKind.EqualsToken) {
    return [];
  }

  if (isModuleExportsExpression(expression.left)) {
    return commonJsModuleExportsEntries(expression.right);
  }

  const exportedName = commonJsNamedExportName(expression.left);
  if (!exportedName) return [];
  const localName = exportedExpressionLocalName(expression.right);
  if (!localName) return [];
  return [
    {
      localName,
      exportedName,
      defaultExported: exportedName === "default",
      referenceNode: expression.left,
    },
  ];
}

function commonJsModuleExportsEntries(expression: ts.Expression): CommonJsExportEntry[] {
  if (ts.isObjectLiteralExpression(expression)) {
    const entries: CommonJsExportEntry[] = [];
    for (const property of expression.properties) {
      if (ts.isShorthandPropertyAssignment(property)) {
        entries.push({
          localName: property.name.text,
          exportedName: property.name.text,
          defaultExported: false,
          referenceNode: property.name,
        });
        continue;
      }
      if (!ts.isPropertyAssignment(property)) continue;
      const exportedName = propertyNameText(property.name);
      const localName = exportedExpressionLocalName(property.initializer);
      if (!exportedName || !localName) continue;
      entries.push({
        localName,
        exportedName,
        defaultExported: exportedName === "default",
        referenceNode: property.name,
      });
    }
    return entries;
  }

  const localName = exportedExpressionLocalName(expression);
  if (!localName) return [];
  return [
    {
      localName,
      exportedName: "default",
      defaultExported: true,
      referenceNode: expression,
    },
  ];
}

function exportedExpressionLocalName(expression: ts.Expression): string | null {
  if (ts.isIdentifier(expression)) return expression.text;
  if (ts.isFunctionExpression(expression) || ts.isClassExpression(expression)) {
    return expression.name?.text ?? null;
  }
  return null;
}

function isModuleExportsExpression(expression: ts.Expression): boolean {
  return (
    ts.isPropertyAccessExpression(expression) &&
    expression.name.text === "exports" &&
    ts.isIdentifier(expression.expression) &&
    expression.expression.text === "module"
  );
}

function commonJsNamedExportName(expression: ts.Expression): string | null {
  if (ts.isPropertyAccessExpression(expression)) {
    if (ts.isIdentifier(expression.expression) && expression.expression.text === "exports") {
      return expression.name.text;
    }
    if (isModuleExportsExpression(expression.expression)) {
      return expression.name.text;
    }
  }
  if (ts.isElementAccessExpression(expression)) {
    if (
      ts.isIdentifier(expression.expression) &&
      expression.expression.text === "exports" &&
      ts.isStringLiteralLike(expression.argumentExpression)
    ) {
      return expression.argumentExpression.text;
    }
    if (isModuleExportsExpression(expression.expression) && ts.isStringLiteralLike(expression.argumentExpression)) {
      return expression.argumentExpression.text;
    }
  }
  return null;
}
