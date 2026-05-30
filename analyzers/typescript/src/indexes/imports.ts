import ts from "typescript";

import { moduleExportNameText, propertyNameText } from "../ast-utils.js";
import { referenceForNode } from "../references/utils.js";
import type {
  DefaultImportIndexEntry,
  ImportIndexEntry,
  NamedImportIndexEntry,
  NamespaceImportIndexEntry,
} from "../types.js";

export function collectImportIndex(repo: string, source: ts.SourceFile): ImportIndexEntry[] {
  const imports: ImportIndexEntry[] = [];
  for (const statement of source.statements) {
    if (ts.isVariableStatement(statement)) {
      imports.push(...collectRequireImportIndex(repo, source, statement));
      continue;
    }
    if (ts.isImportEqualsDeclaration(statement)) {
      const moduleSpecifier = importEqualsModuleSpecifier(statement.moduleReference);
      if (!moduleSpecifier) continue;
      const reference = referenceForNode(repo, source, statement.name, "import");
      imports.push({
        moduleSpecifier,
        defaultImport: {
          localName: statement.name.text,
          reference,
        },
        namespaceImport: {
          localName: statement.name.text,
          reference,
        },
        namedImports: [],
      });
      continue;
    }
    if (!ts.isImportDeclaration(statement)) continue;
    if (!ts.isStringLiteral(statement.moduleSpecifier)) continue;

    const namedImports: NamedImportIndexEntry[] = [];
    const importClause = statement.importClause;
    const defaultImport: DefaultImportIndexEntry | null = importClause?.name
      ? {
          localName: importClause.name.text,
          reference: referenceForNode(repo, source, importClause.name, "import"),
        }
      : null;
    let namespaceImport: NamespaceImportIndexEntry | null = null;
    const namedBindings = importClause?.namedBindings;
    if (namedBindings && ts.isNamedImports(namedBindings)) {
      for (const element of namedBindings.elements) {
        namedImports.push({
          importedName: moduleExportNameText(element.propertyName ?? element.name),
          localName: element.name.text,
          reference: referenceForNode(repo, source, element.name, "import"),
        });
      }
    } else if (namedBindings && ts.isNamespaceImport(namedBindings)) {
      namespaceImport = {
        localName: namedBindings.name.text,
        reference: referenceForNode(repo, source, namedBindings.name, "import"),
      };
    }

    imports.push({
      moduleSpecifier: statement.moduleSpecifier.text,
      defaultImport,
      namespaceImport,
      namedImports,
    });
  }
  imports.push(...collectDynamicImportIndex(repo, source));
  return imports;
}

function collectDynamicImportIndex(repo: string, source: ts.SourceFile): ImportIndexEntry[] {
  const imports: ImportIndexEntry[] = [];
  visit(source);
  return imports;

  function visit(node: ts.Node): void {
    if (ts.isVariableDeclaration(node)) {
      const entry = dynamicImportIndexEntryForVariableDeclaration(repo, source, node);
      if (entry) {
        imports.push(entry);
      }
    }
    ts.forEachChild(node, visit);
  }
}

function dynamicImportIndexEntryForVariableDeclaration(
  repo: string,
  source: ts.SourceFile,
  declaration: ts.VariableDeclaration,
): ImportIndexEntry | null {
  const moduleSpecifier = dynamicImportModuleSpecifier(declaration.initializer);
  if (!moduleSpecifier) return null;

  if (ts.isIdentifier(declaration.name)) {
    return {
      moduleSpecifier,
      defaultImport: null,
      namespaceImport: {
        localName: declaration.name.text,
        reference: referenceForNode(repo, source, declaration.name, "import"),
      },
      namedImports: [],
    };
  }

  if (ts.isObjectBindingPattern(declaration.name)) {
    const namedImports = namedImportsForRequireBinding(repo, source, declaration.name);
    if (namedImports.length === 0) return null;
    return {
      moduleSpecifier,
      defaultImport: null,
      namespaceImport: null,
      namedImports,
    };
  }

  return null;
}

function dynamicImportModuleSpecifier(expression: ts.Expression | undefined): string | null {
  const unwrapped = unwrapDynamicImportExpression(expression);
  if (!unwrapped || !ts.isCallExpression(unwrapped)) return null;
  if (unwrapped.expression.kind !== ts.SyntaxKind.ImportKeyword) return null;
  const [argument] = unwrapped.arguments;
  if (!argument || !(ts.isStringLiteral(argument) || ts.isNoSubstitutionTemplateLiteral(argument))) return null;
  return argument.text;
}

function unwrapDynamicImportExpression(expression: ts.Expression | undefined): ts.Expression | null {
  if (!expression) return null;
  let current = expression;
  while (true) {
    if (ts.isAwaitExpression(current)) {
      current = current.expression;
      continue;
    }
    if (
      ts.isParenthesizedExpression(current) ||
      ts.isAsExpression(current) ||
      ts.isSatisfiesExpression(current) ||
      ts.isNonNullExpression(current) ||
      ts.isTypeAssertionExpression(current)
    ) {
      current = current.expression;
      continue;
    }
    return current;
  }
}

function importEqualsModuleSpecifier(moduleReference: ts.ModuleReference): string | null {
  if (!ts.isExternalModuleReference(moduleReference)) return null;
  const expression = moduleReference.expression;
  if (ts.isStringLiteral(expression) || ts.isNoSubstitutionTemplateLiteral(expression)) {
    return expression.text;
  }
  return null;
}

function collectRequireImportIndex(
  repo: string,
  source: ts.SourceFile,
  statement: ts.VariableStatement,
): ImportIndexEntry[] {
  const imports: ImportIndexEntry[] = [];
  for (const declaration of statement.declarationList.declarations) {
    const propertyRequire = requirePropertyAccess(declaration.initializer);
    if (propertyRequire && ts.isIdentifier(declaration.name)) {
      imports.push({
        moduleSpecifier: propertyRequire.moduleSpecifier,
        defaultImport: null,
        namespaceImport: null,
        namedImports: [
          {
            importedName: propertyRequire.importedName,
            localName: declaration.name.text,
            reference: referenceForNode(repo, source, declaration.name, "import"),
          },
        ],
      });
      continue;
    }

    const moduleSpecifier = requireModuleSpecifier(declaration.initializer);
    if (!moduleSpecifier) continue;

    if (ts.isIdentifier(declaration.name)) {
      const reference = referenceForNode(repo, source, declaration.name, "import");
      imports.push({
        moduleSpecifier,
        defaultImport: {
          localName: declaration.name.text,
          reference,
        },
        namespaceImport: {
          localName: declaration.name.text,
          reference,
        },
        namedImports: [],
      });
      continue;
    }

    if (ts.isObjectBindingPattern(declaration.name)) {
      const namedImports = namedImportsForRequireBinding(repo, source, declaration.name);
      if (namedImports.length === 0) continue;
      imports.push({
        moduleSpecifier,
        defaultImport: null,
        namespaceImport: null,
        namedImports,
      });
    }
  }
  return imports;
}

function namedImportsForRequireBinding(
  repo: string,
  source: ts.SourceFile,
  binding: ts.ObjectBindingPattern,
): NamedImportIndexEntry[] {
  const namedImports: NamedImportIndexEntry[] = [];
  for (const element of binding.elements) {
    if (!ts.isIdentifier(element.name)) continue;
    const importedName = element.propertyName
      ? propertyNameText(element.propertyName)
      : element.name.text;
    if (!importedName) continue;
    namedImports.push({
      importedName,
      localName: element.name.text,
      reference: referenceForNode(repo, source, element.name, "import"),
    });
  }
  return namedImports;
}

function requirePropertyAccess(
  expression: ts.Expression | undefined,
): { moduleSpecifier: string; importedName: string } | null {
  if (!expression || !ts.isPropertyAccessExpression(expression)) return null;
  const moduleSpecifier = requireModuleSpecifier(expression.expression);
  if (!moduleSpecifier) return null;
  return { moduleSpecifier, importedName: expression.name.text };
}

function requireModuleSpecifier(expression: ts.Expression | undefined): string | null {
  if (!expression || !ts.isCallExpression(expression)) return null;
  if (!ts.isIdentifier(expression.expression) || expression.expression.text !== "require") return null;
  const [argument] = expression.arguments;
  if (!argument || !(ts.isStringLiteral(argument) || ts.isNoSubstitutionTemplateLiteral(argument))) return null;
  return argument.text;
}
