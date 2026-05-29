import path from "node:path";

import ts from "typescript";

import {
  entityNameText,
  expressionNameText,
  nodeLineRange,
  propertyNameText,
  unwrapExpression,
} from "./ast-utils.js";
import { collectDiInjectionIndex, collectDiProviderIndex } from "./di-index.js";
import { collectExportIndex, collectImportIndex } from "./import-export-index.js";
import { hasAncestor, referenceForIdentifier, referenceForNode } from "./reference-utils.js";
import type {
  ClassHeritageIndexEntry,
  IdentifierIndexEntry,
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
