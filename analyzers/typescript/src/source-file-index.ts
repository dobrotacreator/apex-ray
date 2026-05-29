import path from "node:path";

import ts from "typescript";

import {
  arrayLiteralExpressionForInitializer,
  entityNameText,
  expressionNameText,
  identifierFromExpression,
  identifiersFromArrayExpression,
  moduleExportNameText,
  nodeLineRange,
  propertyAssignmentNamed,
  propertyNameText,
  unwrapExpression,
} from "./ast-utils.js";
import { NAMESPACE_EXPORT_LOCAL_NAME, STAR_EXPORT_LOCAL_NAME } from "./constants.js";
import { hasAncestor, referenceForIdentifier, referenceForNode } from "./reference-utils.js";
import type {
  ClassHeritageIndexEntry,
  CommonJsExportEntry,
  DefaultImportIndexEntry,
  DiInjectionIndexEntry,
  DiProviderIndexEntry,
  ExportIndexEntry,
  IdentifierIndexEntry,
  ImportIndexEntry,
  NamedImportIndexEntry,
  NamespaceImportIndexEntry,
  ReceiverIndexEntry,
  ReferenceKind,
  RepoFileIndexEntry,
  TypeAliasIndexEntry,
} from "./types.js";
import { normalizeRelPath, scriptKindForPath } from "./utils.js";

interface SourceFileIndexInput {
  repo: string;
  absPath: string;
  relPath: string;
  size: number;
  mtimeMs: number;
  text: string;
}

export function isAnalyzableSourceFile(filePath: string): boolean {
  const normalized = normalizeRelPath(filePath);
  return /\.(ts|tsx|js|jsx)$/.test(normalized) && !/\.d\.ts$/.test(normalized);
}

export function indexSourceFile(input: SourceFileIndexInput): RepoFileIndexEntry {
  const source = ts.createSourceFile(input.absPath, input.text, ts.ScriptTarget.ES2022, true, scriptKindForPath(input.absPath));
  return {
    absPath: path.resolve(input.absPath),
    relPath: input.relPath,
    relLower: input.relPath.toLowerCase(),
    size: input.size,
    mtimeMs: input.mtimeMs,
    imports: collectImportIndex(input.repo, source),
    exports: collectExportIndex(input.repo, source),
    identifiers: collectIdentifierIndex(input.repo, source),
    receivers: collectReceiverIndex(input.repo, source),
    typeAliases: collectTypeAliasIndex(source),
    classHeritages: collectClassHeritageIndex(source),
    diProviders: collectDiProviderIndex(input.repo, source),
    diInjections: collectDiInjectionIndex(input.repo, source),
  };
}

function collectImportIndex(repo: string, source: ts.SourceFile): ImportIndexEntry[] {
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

function collectExportIndex(repo: string, source: ts.SourceFile): ExportIndexEntry[] {
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

function collectIdentifierIndex(repo: string, source: ts.SourceFile): IdentifierIndexEntry[] {
  const identifiers: IdentifierIndexEntry[] = [];
  visit(source);
  return identifiers;

  function visit(node: ts.Node): void {
    if (ts.isIdentifier(node) && !hasAncestor(node, ts.isImportDeclaration)) {
      identifiers.push({
        name: node.text,
        namespaceQualifier: namespaceQualifierForIdentifier(node),
        reference: referenceForIdentifier(repo, source, node),
      });
    }
    if (ts.isStringLiteralLike(node)) {
      const namespaceQualifier = namespaceQualifierForElementAccessArgument(node);
      if (namespaceQualifier) {
        identifiers.push({
          name: node.text,
          namespaceQualifier,
          reference: referenceForNode(repo, source, node, referenceKindForElementAccessArgument(node)),
        });
      }
    }
    ts.forEachChild(node, visit);
  }
}

function collectReceiverIndex(repo: string, source: ts.SourceFile): ReceiverIndexEntry[] {
  const receivers: ReceiverIndexEntry[] = [];
  const seen = new Set<string>();
  visit(source);
  return receivers;

  function add(receiverName: string | null, typeName: string | null, node: ts.Node, scopeNode?: ts.Node): void {
    if (!receiverName) return;
    const scope = receiverScopeLines(source, scopeNode ?? node);
    const reference = referenceForNode(repo, source, node, "type");
    const key = `${receiverName}:${typeName ?? ""}:${scope.startLine}:${scope.endLine}:${reference.file}:${reference.line}`;
    if (seen.has(key)) return;
    seen.add(key);
    receivers.push({ receiverName, typeName, startLine: scope.startLine, endLine: scope.endLine, reference });
  }

  function visit(node: ts.Node): void {
    if (ts.isClassDeclaration(node) && node.name) {
      add("this", node.name.text, node.name, node);
      const extended = classExtendedTypeName(node);
      if (extended) {
        add("super", extended, node.name, node);
      }
    } else if (ts.isParameter(node) && ts.isIdentifier(node.name)) {
      const typeName = node.type ? typeNameForReceiver(node.type) : null;
      add(node.name.text, typeName, node.name, parameterReceiverScope(node));
      if (isParameterProperty(node)) {
        add(`this.${node.name.text}`, typeName, node.name, enclosingClassDeclaration(node) ?? node.parent);
      }
    } else if (ts.isPropertyDeclaration(node)) {
      const name = propertyNameText(node.name);
      const typeName = node.type
        ? typeNameForReceiver(node.type)
        : newExpressionTypeName(unwrapExpression(node.initializer));
      add(name ? `this.${name}` : null, typeName, node.name, enclosingClassDeclaration(node) ?? node);
    } else if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)) {
      const typeName = node.type
        ? typeNameForReceiver(node.type)
        : newExpressionTypeName(unwrapExpression(node.initializer));
      add(node.name.text, typeName, node.name);
    }
    ts.forEachChild(node, visit);
  }
}

function collectTypeAliasIndex(source: ts.SourceFile): TypeAliasIndexEntry[] {
  const aliases: TypeAliasIndexEntry[] = [];
  for (const statement of source.statements) {
    if (!ts.isTypeAliasDeclaration(statement)) continue;
    const targetName = typeNameForReceiver(statement.type);
    if (!targetName) continue;
    aliases.push({ name: statement.name.text, targetName });
  }
  return aliases;
}

function collectClassHeritageIndex(source: ts.SourceFile): ClassHeritageIndexEntry[] {
  const heritages: ClassHeritageIndexEntry[] = [];
  visit(source);
  return heritages;

  function visit(node: ts.Node): void {
    if ((ts.isClassDeclaration(node) || ts.isInterfaceDeclaration(node)) && node.name) {
      const baseNames = heritageTypeNames(node);
      if (baseNames.length > 0) {
        heritages.push({ className: node.name.text, baseNames });
      }
    }
    ts.forEachChild(node, visit);
  }
}

function heritageTypeNames(node: ts.ClassDeclaration | ts.InterfaceDeclaration): string[] {
  const names: string[] = [];
  for (const clause of node.heritageClauses ?? []) {
    for (const heritageType of clause.types) {
      const name = expressionNameText(heritageType.expression);
      if (name) names.push(name);
    }
  }
  return names;
}

function classExtendedTypeName(node: ts.ClassDeclaration): string | null {
  for (const clause of node.heritageClauses ?? []) {
    if (clause.token !== ts.SyntaxKind.ExtendsKeyword) continue;
    const [extended] = clause.types;
    if (!extended) return null;
    return expressionNameText(extended.expression);
  }
  return null;
}

function parameterReceiverScope(node: ts.ParameterDeclaration): ts.Node {
  const parent = node.parent;
  if (
    (ts.isFunctionDeclaration(parent) ||
      ts.isFunctionExpression(parent) ||
      ts.isArrowFunction(parent) ||
      ts.isMethodDeclaration(parent) ||
      ts.isConstructorDeclaration(parent)) &&
    parent.body
  ) {
    return parent.body;
  }
  return parent;
}

function enclosingClassDeclaration(node: ts.Node): ts.ClassDeclaration | null {
  let current: ts.Node | undefined = node.parent;
  while (current) {
    if (ts.isClassDeclaration(current)) return current;
    if (ts.isSourceFile(current)) return null;
    current = current.parent;
  }
  return null;
}

function receiverScopeLines(source: ts.SourceFile, node: ts.Node): { startLine: number; endLine: number } {
  let current: ts.Node | undefined = node;
  while (current) {
    if (ts.isBlock(current) || ts.isClassDeclaration(current) || ts.isSourceFile(current)) {
      return nodeLineRange(source, current);
    }
    current = current.parent;
  }
  return nodeLineRange(source, source);
}

function isParameterProperty(node: ts.ParameterDeclaration): boolean {
  const modifiers = ts.canHaveModifiers(node) ? ts.getModifiers(node) ?? [] : [];
  return modifiers.some((modifier) =>
    [
      ts.SyntaxKind.PublicKeyword,
      ts.SyntaxKind.PrivateKeyword,
      ts.SyntaxKind.ProtectedKeyword,
      ts.SyntaxKind.ReadonlyKeyword,
    ].includes(modifier.kind),
  );
}

function typeNameForReceiver(type: ts.TypeNode): string | null {
  if (ts.isTypeReferenceNode(type)) {
    return entityNameText(type.typeName);
  }
  return null;
}

function newExpressionTypeName(expression: ts.Expression | null): string | null {
  if (!expression || !ts.isNewExpression(expression)) return null;
  return expressionNameText(expression.expression);
}

function collectDiProviderIndex(repo: string, source: ts.SourceFile): DiProviderIndexEntry[] {
  const providers: DiProviderIndexEntry[] = [];
  const providerArrays = collectDiProviderArrays(repo, source);
  for (const entries of providerArrays.values()) {
    providers.push(...entries);
  }
  visit(source);
  return providers;

  function visit(node: ts.Node): void {
    if (ts.isObjectLiteralExpression(node)) {
      providers.push(...diProviderEntriesForModuleObject(repo, source, node, providerArrays));
      providers.push(...diProviderEntriesForObjectLiteral(repo, source, node));
    }
    ts.forEachChild(node, visit);
  }
}

function collectDiProviderArrays(repo: string, source: ts.SourceFile): Map<string, DiProviderIndexEntry[]> {
  const providerArrays = new Map<string, DiProviderIndexEntry[]>();
  for (const statement of source.statements) {
    if (!ts.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (!ts.isIdentifier(declaration.name)) continue;
      const arrayName = declaration.name.text;
      const array = arrayLiteralExpressionForInitializer(declaration.initializer);
      if (!array) continue;

      const entries: DiProviderIndexEntry[] = [];
      for (const element of array.elements) {
        const unwrapped = ts.isSpreadElement(element) ? unwrapExpression(element.expression) : unwrapExpression(element);
        if (!unwrapped) continue;
        if (ts.isIdentifier(unwrapped)) {
          entries.push({
            tokenName: unwrapped.text,
            implementationName: unwrapped.text,
            reference: referenceForNode(repo, source, unwrapped, "read"),
            sourceArrayName: arrayName,
          });
          continue;
        }
        if (!ts.isObjectLiteralExpression(unwrapped)) continue;
        entries.push(
          ...diProviderEntriesForObjectLiteral(repo, source, unwrapped).map((entry) => ({
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
): DiProviderIndexEntry[] {
  const referenceNode = moduleDecoratorForObjectLiteral(object);
  if (!referenceNode) return [];

  const entries: DiProviderIndexEntry[] = [];
  for (const propertyName of ["providers", "exports"]) {
    const property = propertyAssignmentNamed(object, propertyName);
    const array = property ? unwrapExpression(property.initializer) : null;
    if (!array || !ts.isArrayLiteralExpression(array)) continue;

    for (const element of array.elements) {
      if (ts.isSpreadElement(element)) {
        const spreadIdentifier = identifierFromExpression(element.expression);
        const spreadProviders = spreadIdentifier ? providerArrays.get(spreadIdentifier.text) : undefined;
        if (spreadIdentifier && spreadProviders) {
          entries.push({
            tokenName: spreadIdentifier.text,
            implementationName: spreadIdentifier.text,
            reference: referenceForNode(repo, source, referenceNode, "read"),
          });
          for (const provider of spreadProviders) {
            entries.push({
              tokenName: provider.tokenName,
              implementationName: provider.implementationName,
              reference: referenceForNode(repo, source, referenceNode, "read"),
            });
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
            entries.push({
              tokenName: provider.tokenName,
              implementationName: provider.implementationName,
              reference: referenceForNode(repo, source, referenceNode, "read"),
            });
          }
        }
        entries.push({
          tokenName: unwrapped.text,
          implementationName: unwrapped.text,
          reference: referenceForNode(repo, source, referenceNode, "read"),
        });
        continue;
      }
      if (ts.isObjectLiteralExpression(unwrapped)) {
        for (const provider of diProviderEntriesForObjectLiteral(repo, source, unwrapped)) {
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
): DiProviderIndexEntry[] {
  const provideProperty = propertyAssignmentNamed(object, "provide");
  if (!provideProperty) return [];
  const token = identifierFromExpression(provideProperty.initializer);
  if (!token) return [];

  const implementations: ts.Identifier[] = [];
  for (const propertyName of ["useClass", "useExisting"]) {
    const property = propertyAssignmentNamed(object, propertyName);
    const implementation = property ? identifierFromExpression(property.initializer) : null;
    if (implementation) {
      implementations.push(implementation);
    }
  }

  const injectProperty = propertyAssignmentNamed(object, "inject");
  if (injectProperty) {
    implementations.push(...identifiersFromArrayExpression(injectProperty.initializer));
  }

  return implementations.map((implementation) => ({
    tokenName: token.text,
    implementationName: implementation.text,
    reference: referenceForNode(repo, source, implementation, "read"),
  }));
}

function collectDiInjectionIndex(repo: string, source: ts.SourceFile): DiInjectionIndexEntry[] {
  const injections: DiInjectionIndexEntry[] = [];
  visit(source);
  return injections;

  function visit(node: ts.Node): void {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "Inject") {
      const [argument] = node.arguments;
      const token = identifierFromExpression(argument);
      if (token) {
        injections.push({
          tokenName: token.text,
          reference: referenceForNode(repo, source, token, "read"),
        });
      }
    }
    ts.forEachChild(node, visit);
  }
}

function namespaceQualifierForIdentifier(node: ts.Identifier): string | null {
  const parent = node.parent;
  if (ts.isPropertyAccessExpression(parent) && parent.name === node) {
    return propertyAccessExpressionText(parent.expression);
  }
  return null;
}

function namespaceQualifierForElementAccessArgument(node: ts.StringLiteralLike): string | null {
  const parent = node.parent;
  if (!ts.isElementAccessExpression(parent) || parent.argumentExpression !== node) return null;
  return propertyAccessExpressionText(parent.expression);
}

function propertyAccessExpressionText(expression: ts.Expression): string | null {
  if (expression.kind === ts.SyntaxKind.ThisKeyword) return "this";
  if (expression.kind === ts.SyntaxKind.SuperKeyword) return "super";
  if (ts.isIdentifier(expression)) return expression.text;
  if (ts.isPropertyAccessExpression(expression)) {
    const qualifier = propertyAccessExpressionText(expression.expression);
    return qualifier ? `${qualifier}.${expression.name.text}` : null;
  }
  return null;
}

function referenceKindForElementAccessArgument(node: ts.StringLiteralLike): ReferenceKind {
  const parent = node.parent;
  if (ts.isElementAccessExpression(parent) && ts.isBinaryExpression(parent.parent) && parent.parent.left === parent) {
    return "write";
  }
  return "read";
}
