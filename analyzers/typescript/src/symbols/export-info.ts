import ts from "typescript";

import { moduleExportNameText } from "../ast-utils.js";
import { commonJsExportEntries } from "../indexes/import-export.js";
import type { ExportedSymbolInfo } from "../types.js";

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

export function collectExportedSymbolInfo(source: ts.SourceFile): ExportedSymbolInfo {
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
