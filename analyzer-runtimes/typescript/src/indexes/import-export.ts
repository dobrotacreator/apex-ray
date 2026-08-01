import ts from "typescript";

import { moduleExportNameText, propertyNameText } from "../ast-utils.js";
import { NAMESPACE_EXPORT_LOCAL_NAME, STAR_EXPORT_LOCAL_NAME } from "../constants.js";
import { referenceForNode } from "../references/utils.js";
import type { CommonJsExportEntry, ExportIndexEntry } from "../types.js";
import {
  appendIndexEntry,
  collectionShouldStop,
  type IndexCollectionControl,
} from "./collection.js";

export { collectImportIndex } from "./imports.js";

export function collectExportIndex(
  repo: string,
  source: ts.SourceFile,
  control?: IndexCollectionControl,
): ExportIndexEntry[] {
  const exports: ExportIndexEntry[] = [];
  for (const statement of source.statements) {
    if (collectionShouldStop(control)) break;
    if (ts.isExpressionStatement(statement)) {
      for (const entry of commonJsExportEntries(statement.expression, control)) {
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
      if (!appendIndexEntry(exports, {
        moduleSpecifier,
        localName: STAR_EXPORT_LOCAL_NAME,
        exportedName: STAR_EXPORT_LOCAL_NAME,
        reference: referenceForNode(repo, source, moduleSpecifierNode, "import"),
      }, control)) break;
      continue;
    }
    if (ts.isNamespaceExport(statement.exportClause)) {
      if (moduleSpecifier === null) continue;
      if (!appendIndexEntry(exports, {
        moduleSpecifier,
        localName: NAMESPACE_EXPORT_LOCAL_NAME,
        exportedName: statement.exportClause.name.text,
        reference: referenceForNode(repo, source, statement.exportClause.name, "import"),
      }, control)) break;
      continue;
    }

    if (!ts.isNamedExports(statement.exportClause)) continue;
    for (const specifier of statement.exportClause.elements) {
      if (!appendIndexEntry(exports, {
        moduleSpecifier,
        localName: moduleExportNameText(specifier.propertyName ?? specifier.name),
        exportedName: moduleExportNameText(specifier.name),
        reference: referenceForNode(repo, source, specifier.name, "import"),
      }, control)) break;
    }
  }
  return exports;
}

export function commonJsExportEntries(
  expression: ts.Expression,
  control?: IndexCollectionControl,
): CommonJsExportEntry[] {
  if (collectionShouldStop(control)) return [];
  if (!ts.isBinaryExpression(expression) || expression.operatorToken.kind !== ts.SyntaxKind.EqualsToken) {
    return [];
  }

  if (isModuleExportsExpression(expression.left)) {
    return commonJsModuleExportsEntries(expression.right, control);
  }

  const exportedName = commonJsNamedExportName(expression.left);
  if (!exportedName) return [];
  const localName = exportedExpressionLocalName(expression.right);
  if (!localName) return [];
  const entries: CommonJsExportEntry[] = [];
  appendIndexEntry(
    entries,
    {
      localName,
      exportedName,
      defaultExported: exportedName === "default",
      referenceNode: expression.left,
    },
    control,
  );
  return entries;
}

function commonJsModuleExportsEntries(
  expression: ts.Expression,
  control?: IndexCollectionControl,
): CommonJsExportEntry[] {
  if (ts.isObjectLiteralExpression(expression)) {
    const entries: CommonJsExportEntry[] = [];
    for (const property of expression.properties) {
      if (collectionShouldStop(control)) break;
      if (ts.isShorthandPropertyAssignment(property)) {
        if (!appendIndexEntry(entries, {
          localName: property.name.text,
          exportedName: property.name.text,
          defaultExported: false,
          referenceNode: property.name,
        }, control)) break;
        continue;
      }
      if (!ts.isPropertyAssignment(property)) continue;
      const exportedName = propertyNameText(property.name);
      const localName = exportedExpressionLocalName(property.initializer);
      if (!exportedName || !localName) continue;
      if (!appendIndexEntry(entries, {
        localName,
        exportedName,
        defaultExported: exportedName === "default",
        referenceNode: property.name,
      }, control)) break;
    }
    return entries;
  }

  const localName = exportedExpressionLocalName(expression);
  if (!localName) return [];
  const entries: CommonJsExportEntry[] = [];
  appendIndexEntry(
    entries,
    {
      localName,
      exportedName: "default",
      defaultExported: true,
      referenceNode: expression,
    },
    control,
  );
  return entries;
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
