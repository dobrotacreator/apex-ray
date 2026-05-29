import ts from "typescript";
import path from "node:path";

import {
  decoratorsForNode,
  entityNameText,
  expressionNameText,
  identifierFromExpression,
  identifiersFromArrayExpression,
  isObjectFreezeCall,
  nodeLineRange,
  propertyAssignmentNamed,
  propertyNameText,
  unwrapExpression,
} from "./ast-utils.js";
import { canonicalSymbol } from "./checker-utils.js";
import {
  CLASS_HERITAGE_CONTRACT_DEPTH_LIMIT,
  CONTRACT_DEPENDENCY_DEPTH_LIMIT,
  IGNORED_CONTRACT_DEPENDENCY_NAMES,
  NAMESPACE_EXPORT_LOCAL_NAME,
  REFERENCE_COLLECTION_LIMIT,
  REFERENCE_LIMIT,
  REFLECTOR_METADATA_METHOD_NAMES,
  STAR_EXPORT_LOCAL_NAME,
} from "./constants.js";
import {
  findIndexedPackageForFile,
  isModuleSpecifierRelatedToPath,
  moduleSpecifierCandidatePaths,
} from "./module-resolution.js";
import { createProgramContexts } from "./program.js";
import {
  hasAncestor,
  isDeclarationNameIdentifier,
  referenceForIdentifier,
  referenceForNode,
  referenceKind,
} from "./reference-utils.js";
import { addReference, mergeReferences } from "./reference-merge.js";
import { buildRepoIndex } from "./repo-index.js";
import {
  collectDeletedSymbols,
  collectExports,
  collectImports,
  collectSymbols,
  preferSyntheticChildSymbols,
} from "./symbol-collection.js";
import { findRelatedTests, isTestPath } from "./test-discovery.js";
import type {
  AnalyzerResult,
  Args,
  CollectedSymbol,
  ExportedNamesForTarget,
  ExportIndexEntry,
  FileAnalysis,
  IdentifierIndexEntry,
  ImportedBindingsForTarget,
  MetadataKeyIdentity,
  PackageInfo,
  ProgramContext,
  ReceiverIndexEntry,
  Reference,
  ReferenceKind,
  RepoFileIndexEntry,
  RepoIndex,
  TypeAliasIndexEntry,
} from "./types.js";
import {
  formatDiagnostic,
  isInsideRepo,
  isRepoRelativePath,
  normalizeRelPath,
  rangesOverlap,
  sourceFileName,
  walk,
} from "./utils.js";

export type {
  AnalyzerResult,
  AnalyzerSymbol,
  Args,
  DeletedLine,
  FileAnalysis,
  Reference,
  ReferenceKind,
  SymbolKind,
} from "./types.js";

export function analyze(args: Args): AnalyzerResult {
  const warnings: string[] = [];
  const contextsByFile = createProgramContexts(args, warnings);
  const repoIndex = buildRepoIndex(args);

  const files: FileAnalysis[] = [];
  for (const changedFile of args.changed) {
    const context = contextsByFile.get(changedFile);
    if (!context) {
      warnings.push(`No TypeScript program could be created for changed file: ${changedFile}`);
      continue;
    }

    const { program, checker } = context;
    const absPath = path.resolve(args.repo, changedFile);
    const source = program.getSourceFile(absPath);
    if (!source) {
      warnings.push(`Changed file is not part of the TypeScript program: ${changedFile}`);
      continue;
    }

    const collectedSymbols = collectSymbols(source, checker);
    const symbols = collectedSymbols.map((symbol) => symbol.analysis);
    const imports = collectImports(source);
    const exports = collectExports(source);
    const ranges = args.changedRanges.get(changedFile) ?? [];
    const deletedCollectedSymbols = collectDeletedSymbols(
      source,
      collectedSymbols,
      args.deletedLines.get(changedFile) ?? [],
    );
    const changedCollectedSymbols = preferSyntheticChildSymbols([
      ...deletedCollectedSymbols,
      ...collectedSymbols.filter((symbol) =>
        ranges.some(([start, end]) => rangesOverlap(symbol.analysis.startLine, symbol.analysis.endLine, start, end)),
      ),
    ]);
    const isChangedTestFile = isTestPath(changedFile.toLowerCase());

    for (const symbol of changedCollectedSymbols) {
      if (isChangedTestFile) {
        symbol.analysis.references = [];
        symbol.analysis.callees = [];
        symbol.analysis.contracts = [];
        symbol.analysis.metadata = [];
        continue;
      }
      const directReferences = [
        ...collectReferences(program, checker, symbol, args.repo, REFERENCE_COLLECTION_LIMIT),
        ...collectImplementedMemberUsageReferences(program, checker, symbol, args.repo, REFERENCE_COLLECTION_LIMIT),
      ];
      const consumerImpact = !symbol.analysis.name.includes(":")
        ? { references: [], callees: [] }
        : collectReferenceConsumerImpact(program, checker, symbol, args.repo, REFERENCE_COLLECTION_LIMIT);
      symbol.analysis.references = mergeReferences(
        [
          ...directReferences,
          ...consumerImpact.references,
          ...collectWorkspaceImportReferences(args.repo, repoIndex, symbol, REFERENCE_COLLECTION_LIMIT),
          ...collectWorkspaceMemberReferences(args.repo, repoIndex, symbol, REFERENCE_COLLECTION_LIMIT),
          ...collectWorkspaceDiReferences(args.repo, repoIndex, symbol, REFERENCE_COLLECTION_LIMIT),
          ...collectProviderTokenInjectionReferences(args.repo, repoIndex, symbol, REFERENCE_COLLECTION_LIMIT),
        ],
        REFERENCE_LIMIT,
      );
      symbol.analysis.references = filterInvalidWorkspaceMemberReferences(args.repo, repoIndex, symbol, symbol.analysis.references);
      symbol.analysis.callees = mergeReferences(
        [
          ...collectCallees(checker, symbol, args.repo, REFERENCE_COLLECTION_LIMIT),
          ...consumerImpact.callees,
        ],
        REFERENCE_LIMIT,
      );
      symbol.analysis.contracts = mergeReferences(
        collectSchemaContracts(program, checker, symbol, args.repo, REFERENCE_COLLECTION_LIMIT),
        REFERENCE_LIMIT,
      );
      symbol.analysis.metadata = mergeReferences(
        collectFrameworkMetadata(symbol, args.repo, REFERENCE_COLLECTION_LIMIT),
        REFERENCE_LIMIT,
      );
    }

    const changedReferences = changedCollectedSymbols.flatMap((symbol) => symbol.analysis.references);
    files.push({
      path: changedFile,
      tsconfigPath: context.tsconfigPath,
      symbols,
      imports,
      exports,
      relatedTests: findRelatedTests(args.repo, repoIndex, changedFile, changedReferences),
      changedSymbols: changedCollectedSymbols.map((symbol) => symbol.analysis),
    });
  }

  const tsconfigPaths = new Set(files.map((file) => file.tsconfigPath).filter((value): value is string => Boolean(value)));
  return {
    language: "typescript",
    projectRoot: args.repo,
    tsconfigPath: tsconfigPaths.size === 1 ? [...tsconfigPaths][0] : null,
    files,
    warnings,
    indexCache: repoIndex.cacheStats,
  };
}

function collectReferences(
  program: ts.Program,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): Reference[] {
  const refs: Reference[] = [];
  const seen = new Set<string>();
  const targetSource = target.node.getSourceFile();
  const excludedNode = target.containerNode ?? target.node;
  const targetStart = excludedNode.getStart(targetSource);
  const targetEnd = excludedNode.getEnd();
  for (const source of program.getSourceFiles()) {
    if (source.isDeclarationFile) continue;
    if (!isInsideRepo(repo, source.fileName)) continue;
    visit(source);
    if (refs.length >= limit) break;
  }
  return refs;

  function visit(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (ts.isIdentifier(node) && isReferenceToTarget(node, checker, target)) {
      const source = node.getSourceFile();
      if (sourceFileName(source) === sourceFileName(targetSource)) {
        const start = node.getStart(source);
        if (start >= targetStart && start < targetEnd) {
          ts.forEachChild(node, visit);
          return;
        }
      }
      const position = source.getLineAndCharacterOfPosition(node.getStart(source));
      const file = normalizeRelPath(path.relative(repo, source.fileName));
      if (!isRepoRelativePath(file)) {
        ts.forEachChild(node, visit);
        return;
      }
      const text = source.text.split(/\r?\n/)[position.line]?.trim() ?? node.text;
      const kind = referenceKind(node);
      const key = `${file}:${position.line + 1}:${kind}:${text}`;
      if (!seen.has(key)) {
        seen.add(key);
        refs.push({
          file,
          line: position.line + 1,
          text,
          kind,
        });
      }
    }
    ts.forEachChild(node, visit);
  }
}

function collectImplementedMemberUsageReferences(
  program: ts.Program,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): Reference[] {
  if (!ts.isMethodDeclaration(target.node)) return [];
  const methodName = propertyNameText(target.node.name);
  if (!methodName) return [];

  const memberSymbols = implementedMemberSymbols(checker, target);
  if (memberSymbols.size === 0) return [];

  const refs: Reference[] = [];
  const seen = new Set<string>();
  const targetSource = target.node.getSourceFile();
  const excludedNode = target.containerNode ?? target.node;
  const targetStart = excludedNode.getStart(targetSource);
  const targetEnd = excludedNode.getEnd();

  for (const source of program.getSourceFiles()) {
    if (source.isDeclarationFile) continue;
    if (!isInsideRepo(repo, source.fileName)) continue;
    visit(source);
    if (refs.length >= limit) break;
  }
  return refs;

  function visit(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (ts.isIdentifier(node) && node.text === methodName && !isDeclarationNameIdentifier(node)) {
      const source = node.getSourceFile();
      if (sourceFileName(source) === sourceFileName(targetSource)) {
        const start = node.getStart(source);
        if (start >= targetStart && start < targetEnd) {
          ts.forEachChild(node, visit);
          return;
        }
      }
      const nodeSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(node));
      if (nodeSymbol && memberSymbols.has(nodeSymbol)) {
        const file = normalizeRelPath(path.relative(repo, source.fileName));
        if (isRepoRelativePath(file)) {
          addReference(refs, seen, referenceForNode(repo, source, node, referenceKind(node)), limit);
        }
      }
    }
    ts.forEachChild(node, visit);
  }
}

function collectReferenceConsumerImpact(
  program: ts.Program,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): { references: Reference[]; callees: Reference[] } {
  const references: Reference[] = [];
  const callees: Reference[] = [];
  const seenReferences = new Set<string>();
  const seenCallees = new Set<string>();
  const consumerSymbols = new Set<ts.Symbol>();
  const consumerNames = new Set<string>();
  const targetSource = target.node.getSourceFile();
  const excludedNode = target.containerNode ?? target.node;

  for (const source of program.getSourceFiles()) {
    if (source.isDeclarationFile || !isInsideRepo(repo, source.fileName)) continue;
    collectConsumerSymbols(source);
  }

  for (const symbol of consumerSymbols) {
    if (references.length >= limit && callees.length >= limit) break;
    collectConsumerReferences(symbol, null);
  }
  for (const name of consumerNames) {
    if (references.length >= limit && callees.length >= limit) break;
    collectConsumerReferences(null, name);
  }

  return { references, callees };

  function collectConsumerSymbols(source: ts.SourceFile): void {
    visit(source);

    function visit(node: ts.Node): void {
      if (ts.isIdentifier(node) && isReferenceToTarget(node, checker, target)) {
        if (isNodeInsideTarget(node, excludedNode, targetSource)) {
          ts.forEachChild(node, visit);
          return;
        }
        const declaration = enclosingImpactDeclaration(node);
        const symbol = declaration ? symbolForDeclaration(checker, declaration) : null;
        const name = declaration ? declarationName(declaration) : null;
        const nameText = name ? name.getText(declaration?.getSourceFile()) : null;
        const sameNameAsTarget = nameText === target.analysis.name;
        if (declaration && symbol && !sameNameAsTarget && !isDeclarationInsideTarget(declaration, excludedNode, targetSource)) {
          consumerSymbols.add(symbol);
        }
        if (declaration && name && !sameNameAsTarget && !isDeclarationInsideTarget(declaration, excludedNode, targetSource)) {
          consumerNames.add(name.getText(declaration.getSourceFile()));
        }
      }
      ts.forEachChild(node, visit);
    }
  }

  function collectConsumerReferences(consumerSymbol: ts.Symbol | null, consumerName: string | null): void {
    for (const source of program.getSourceFiles()) {
      if (source.isDeclarationFile || !isInsideRepo(repo, source.fileName)) continue;
      visit(source);
      if (references.length >= limit && callees.length >= limit) return;
    }

    function visit(node: ts.Node): void {
      if (references.length < limit && ts.isIdentifier(node) && !isDeclarationNameIdentifier(node)) {
        const nodeSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(node));
        const matchesSymbol =
          consumerSymbol !== null && nodeSymbol === consumerSymbol && !symbolHasDeclarationContainingNode(consumerSymbol, node);
        const matchesName = consumerName !== null && node.text === consumerName;
        if ((matchesSymbol || matchesName) && isValidConsumerReferenceNode(node)) {
          const source = node.getSourceFile();
          const file = normalizeRelPath(path.relative(repo, source.fileName));
          if (isRepoRelativePath(file)) {
            addReference(references, seenReferences, referenceForNode(repo, source, node, referenceKind(node)), limit);
            const callerDeclaration = enclosingImpactDeclaration(node);
            const callerSymbol = callerDeclaration ? symbolForDeclaration(checker, callerDeclaration) : null;
            const callerName = callerDeclaration ? declarationName(callerDeclaration)?.getText(callerDeclaration.getSourceFile()) : null;
            if (
              callerDeclaration &&
              (consumerSymbol === null || callerSymbol !== consumerSymbol) &&
              (consumerName === null || callerName !== consumerName)
            ) {
              collectCalleesFromNode(checker, callerDeclaration, repo, limit, callees, seenCallees);
            }
          }
        }
      }
      ts.forEachChild(node, visit);
    }
  }

  function isValidConsumerReferenceNode(node: ts.Identifier): boolean {
    if (!isPropertyAccessMemberName(node)) return true;
    if (node.text !== target.analysis.name) return true;
    return isReferenceToTarget(node, checker, target);
  }
}

function collectCallees(
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): Reference[] {
  const refs: Reference[] = [];
  const seen = new Set<string>();
  collectCalleesFromNode(checker, target.node, repo, limit, refs, seen, target.node);
  return refs;
}

function collectCalleesFromNode(
  checker: ts.TypeChecker,
  node: ts.Node,
  repo: string,
  limit: number,
  refs: Reference[],
  seen: Set<string>,
  excludedNode?: ts.Node,
): void {
  const targetSource = node.getSourceFile();
  visit(node);

  function visit(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (ts.isCallExpression(node)) {
      const calleeNode = calleeNameNode(node.expression);
      const calleeSymbol = calleeNode ? canonicalSymbol(checker, checker.getSymbolAtLocation(calleeNode)) : null;
      if (calleeSymbol) {
        for (const declaration of calleeSymbol.declarations ?? []) {
          if (refs.length >= limit) break;
          if (excludedNode && isDeclarationInsideTarget(declaration, excludedNode, targetSource)) continue;
          const source = declaration.getSourceFile();
          if (source.isDeclarationFile || !isInsideRepo(repo, source.fileName)) continue;
          const file = normalizeRelPath(path.relative(repo, source.fileName));
          if (!isRepoRelativePath(file)) continue;
          addReference(refs, seen, referenceForNode(repo, source, declaration, "callee"), limit);
        }
      }
    }
    ts.forEachChild(node, visit);
  }
}

function calleeNameNode(expression: ts.Expression): ts.Node | null {
  if (ts.isIdentifier(expression)) return expression;
  if (ts.isPropertyAccessExpression(expression)) return expression.name;
  return null;
}

function collectSchemaContracts(
  program: ts.Program,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): Reference[] {
  const refs: Reference[] = [];
  const seen = new Set<string>();
  const contractSymbolsSeen = new Set<ts.Symbol>();
  collectDeclaredTypeContracts(refs, seen, contractSymbolsSeen, checker, target, repo, limit);
  collectClassHeritageContracts(refs, seen, checker, target, repo, limit);
  collectDecoratorArgumentContracts(refs, seen, checker, target, repo, limit);
  collectDecoratorMetadataKeyConsumerContracts(refs, seen, program, checker, target, repo, limit);
  collectImplementedMemberContracts(refs, seen, checker, target, repo, limit);
  visit(target.node);
  return refs;

  function visit(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (ts.isCallExpression(node)) {
      const schemaNode = schemaReceiverNameNode(node.expression);
      const schemaSymbol = schemaNode ? canonicalSymbol(checker, checker.getSymbolAtLocation(schemaNode)) : null;
      addContractSymbolWithDependencies(refs, seen, contractSymbolsSeen, checker, schemaSymbol, target.node, repo, limit, 0);
      for (const argument of node.arguments) {
        if (refs.length >= limit) break;
        const schemaArgumentNode = schemaArgumentNameNode(argument);
        const schemaArgumentSymbol = schemaArgumentNode
          ? canonicalSymbol(checker, checker.getSymbolAtLocation(schemaArgumentNode))
          : null;
        addContractSymbolWithDependencies(refs, seen, contractSymbolsSeen, checker, schemaArgumentSymbol, target.node, repo, limit, 0);
      }
    }
    ts.forEachChild(node, visit);
  }
}

function collectClassHeritageContracts(
  refs: Reference[],
  seen: Set<string>,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): void {
  if (!ts.isClassDeclaration(target.node)) return;
  const seenSymbols = new Set<ts.Symbol>();
  collectHeritageDeclarations(target.node, 0);

  function collectHeritageDeclarations(
    declaration: ts.ClassDeclaration | ts.InterfaceDeclaration,
    depth: number,
  ): void {
    if (depth >= CLASS_HERITAGE_CONTRACT_DEPTH_LIMIT || refs.length >= limit) return;

    for (const clause of declaration.heritageClauses ?? []) {
      if (refs.length >= limit) return;
      if (clause.token !== ts.SyntaxKind.ImplementsKeyword && clause.token !== ts.SyntaxKind.ExtendsKeyword) continue;

      for (const heritageType of clause.types) {
        if (refs.length >= limit) return;
        const contractSymbol = symbolForHeritageType(checker, heritageType);
        if (!contractSymbol || seenSymbols.has(contractSymbol)) continue;
        seenSymbols.add(contractSymbol);
        addContractDeclarationReferences(refs, seen, contractSymbol, target.node, repo, limit);

        for (const contractDeclaration of contractSymbol.declarations ?? []) {
          if (refs.length >= limit) return;
          if (ts.isClassDeclaration(contractDeclaration) || ts.isInterfaceDeclaration(contractDeclaration)) {
            collectHeritageDeclarations(contractDeclaration, depth + 1);
          }
        }
      }
    }
  }
}

function symbolForHeritageType(checker: ts.TypeChecker, heritageType: ts.ExpressionWithTypeArguments): ts.Symbol | null {
  const expressionSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(heritageType.expression));
  if (expressionSymbol) return expressionSymbol;
  return canonicalSymbol(checker, checker.getTypeAtLocation(heritageType).symbol);
}

function collectDecoratorArgumentContracts(
  refs: Reference[],
  seen: Set<string>,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): void {
  for (const node of metadataNodesForTarget(target)) {
    if (refs.length >= limit) return;
    for (const decorator of decoratorsForNode(node)) {
      if (refs.length >= limit) return;
      const decoratorName = decoratorNameNode(decorator);
      const decoratorSymbol = decoratorName ? canonicalSymbol(checker, checker.getSymbolAtLocation(decoratorName)) : null;
      addContractDeclarationReferences(refs, seen, decoratorSymbol, target.node, repo, limit);
      for (const argument of decoratorArgumentExpressions(decorator)) {
        visitArgument(argument);
      }
    }
  }

  function visitArgument(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (ts.isIdentifier(node)) {
      const symbol = canonicalSymbol(checker, checker.getSymbolAtLocation(node));
      addContractDeclarationReferences(refs, seen, symbol, target.node, repo, limit);
    }
    ts.forEachChild(node, visitArgument);
  }
}

function collectDecoratorMetadataKeyConsumerContracts(
  refs: Reference[],
  seen: Set<string>,
  program: ts.Program,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): void {
  const keys = metadataKeysForTargetDecorators(checker, target);
  if (keys.length === 0) return;

  for (const source of program.getSourceFiles()) {
    if (refs.length >= limit) return;
    if (source.isDeclarationFile || !isInsideRepo(repo, source.fileName)) continue;
    visit(source);
  }

  function visit(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (ts.isCallExpression(node) && isReflectorMetadataConsumerCall(node)) {
      const keyArgument = node.arguments[0];
      if (keyArgument && metadataKeyMatches(checker, keyArgument, keys)) {
        const declaration = enclosingMetadataConsumerDeclaration(node);
        if (declaration && !isDeclarationInsideTarget(declaration, target.node, target.node.getSourceFile())) {
          addReference(refs, seen, referenceForNode(repo, declaration.getSourceFile(), declaration, "contract"), limit);
        }
      }
    }
    ts.forEachChild(node, visit);
  }
}

function metadataKeysForTargetDecorators(checker: ts.TypeChecker, target: CollectedSymbol): MetadataKeyIdentity[] {
  const keys: MetadataKeyIdentity[] = [];
  const seen = new Set<string>();
  for (const node of metadataNodesForTarget(target)) {
    for (const decorator of decoratorsForNode(node)) {
      const decoratorName = decoratorNameNode(decorator);
      const decoratorSymbol = decoratorName ? canonicalSymbol(checker, checker.getSymbolAtLocation(decoratorName)) : null;
      if (!decoratorSymbol) continue;
      for (const declaration of decoratorSymbol.declarations ?? []) {
        collectMetadataProducerKeysFromDeclaration(keys, seen, checker, declaration);
      }
    }
  }
  return keys;
}

function collectMetadataProducerKeysFromDeclaration(
  keys: MetadataKeyIdentity[],
  seen: Set<string>,
  checker: ts.TypeChecker,
  declaration: ts.Declaration,
): void {
  visit(declaration);

  function visit(node: ts.Node): void {
    if (ts.isCallExpression(node) && isSetMetadataCall(node)) {
      const key = metadataKeyIdentityForExpression(checker, node.arguments[0]);
      addMetadataKeyIdentity(keys, seen, key);
    }
    ts.forEachChild(node, visit);
  }
}

function isSetMetadataCall(node: ts.CallExpression): boolean {
  const callee = calleeNameNode(node.expression);
  return Boolean(callee && ts.isIdentifier(callee) && callee.text === "SetMetadata");
}

function addMetadataKeyIdentity(keys: MetadataKeyIdentity[], seen: Set<string>, key: MetadataKeyIdentity | null): void {
  if (!key) return;
  const identityKey = key.symbol
    ? `symbol:${key.symbol.name}:${key.symbol.declarations?.[0]?.getSourceFile().fileName ?? ""}`
    : `text:${key.text}`;
  if (seen.has(identityKey)) return;
  seen.add(identityKey);
  keys.push(key);
}

function metadataKeyMatches(
  checker: ts.TypeChecker,
  expression: ts.Expression,
  keys: MetadataKeyIdentity[],
): boolean {
  const candidate = metadataKeyIdentityForExpression(checker, expression);
  if (!candidate) return false;
  return keys.some((key) => metadataKeyIdentitiesMatch(key, candidate));
}

function metadataKeyIdentitiesMatch(left: MetadataKeyIdentity, right: MetadataKeyIdentity): boolean {
  if (left.symbol && right.symbol && left.symbol === right.symbol) return true;
  if (left.text && right.text && left.text === right.text) return true;
  return false;
}

function metadataKeyIdentityForExpression(
  checker: ts.TypeChecker,
  expression: ts.Expression | undefined,
): MetadataKeyIdentity | null {
  const unwrapped = unwrapExpression(expression);
  if (!unwrapped) return null;
  const symbolNode = metadataKeySymbolNode(unwrapped);
  const symbol = symbolNode ? canonicalSymbol(checker, checker.getSymbolAtLocation(symbolNode)) : null;
  const text = metadataKeyComparableText(unwrapped, symbolNode);
  if (!symbol && !text) return null;
  return { symbol, text };
}

function metadataKeySymbolNode(expression: ts.Expression): ts.Node | null {
  if (ts.isIdentifier(expression)) return expression;
  if (ts.isPropertyAccessExpression(expression)) return expression.name;
  if (ts.isElementAccessExpression(expression) && expression.argumentExpression) {
    return expression.argumentExpression;
  }
  return null;
}

function metadataKeyComparableText(expression: ts.Expression, symbolNode: ts.Node | null): string | null {
  if (ts.isStringLiteralLike(expression)) return expression.text;
  if (ts.isNumericLiteral(expression)) return expression.text;
  return symbolNode?.getText() ?? expression.getText();
}

function isReflectorMetadataConsumerCall(node: ts.CallExpression): boolean {
  const expression = node.expression;
  if (!ts.isPropertyAccessExpression(expression)) return false;
  const methodName = expression.name.text;
  if (!REFLECTOR_METADATA_METHOD_NAMES.has(methodName)) return false;
  return methodName !== "get" || isReflectorLikeReceiver(expression.expression);
}

function isReflectorLikeReceiver(expression: ts.Expression): boolean {
  if (ts.isIdentifier(expression)) return expression.text.toLowerCase().includes("reflector");
  if (ts.isPropertyAccessExpression(expression)) {
    return expression.name.text.toLowerCase().includes("reflector") || isReflectorLikeReceiver(expression.expression);
  }
  return false;
}

function enclosingMetadataConsumerDeclaration(node: ts.Node): ts.Declaration | null {
  let current: ts.Node | undefined = node;
  let fallback: ts.Declaration | null = null;
  while (current) {
    if (ts.isClassDeclaration(current) && current.name) return current;
    if (
      !fallback &&
      (ts.isMethodDeclaration(current) ||
        ts.isFunctionDeclaration(current) ||
        ts.isVariableDeclaration(current))
    ) {
      fallback = current;
    }
    current = current.parent;
  }
  return fallback;
}

function decoratorNameNode(decorator: ts.Decorator): ts.Node | null {
  const expression = decorator.expression;
  if (ts.isCallExpression(expression)) return calleeNameNode(expression.expression);
  if (ts.isIdentifier(expression)) return expression;
  if (ts.isPropertyAccessExpression(expression)) return expression.name;
  return null;
}

function decoratorArgumentExpressions(decorator: ts.Decorator): readonly ts.Expression[] {
  const expression = decorator.expression;
  return ts.isCallExpression(expression) ? expression.arguments : [];
}

function collectDeclaredTypeContracts(
  refs: Reference[],
  seen: Set<string>,
  contractSymbolsSeen: Set<ts.Symbol>,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): void {
  for (const parameter of parametersForNode(target.node)) {
    if (!parameter.type) continue;
    visitType(parameter.type);
    if (refs.length >= limit) return;
  }

  const returnType = returnTypeForNode(target.node);
  if (returnType) {
    visitType(returnType);
  }

  for (const typeNode of variableTypeNodesForTarget(target)) {
    if (refs.length >= limit) return;
    visitType(typeNode);
  }

  function visitType(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (ts.isTypeReferenceNode(node)) {
      const symbolNode = entityNameLeaf(node.typeName);
      const typeSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(symbolNode));
      addContractSymbolWithDependencies(refs, seen, contractSymbolsSeen, checker, typeSymbol, target.node, repo, limit, 0);
    }
    if (ts.isTypeQueryNode(node)) {
      const symbolNode = entityNameLeaf(node.exprName);
      const valueSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(symbolNode));
      addContractSymbolWithDependencies(refs, seen, contractSymbolsSeen, checker, valueSymbol, target.node, repo, limit, 0);
    }
    ts.forEachChild(node, visitType);
  }
}

function returnTypeForNode(node: ts.Node): ts.TypeNode | null {
  if (
    ts.isMethodDeclaration(node) ||
    ts.isFunctionDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isArrowFunction(node)
  ) {
    return node.type ?? null;
  }
  return null;
}

function variableTypeNodesForTarget(target: CollectedSymbol): ts.TypeNode[] {
  const declaration = variableDeclarationForNode(target.node) ??
    (target.containerNode ? variableDeclarationForNode(target.containerNode) : null);
  if (!declaration) return [];

  const typeNodes: ts.TypeNode[] = [];
  if (declaration.type) {
    typeNodes.push(declaration.type);
  }
  typeNodes.push(...expressionTypeContextNodes(declaration.initializer));
  return typeNodes;
}

function variableDeclarationForNode(node: ts.Node): ts.VariableDeclaration | null {
  let current: ts.Node | undefined = node;
  while (current) {
    if (ts.isVariableDeclaration(current)) return current;
    if (ts.isVariableStatement(current)) return current.declarationList.declarations[0] ?? null;
    if (ts.isSourceFile(current)) return null;
    current = current.parent;
  }
  return null;
}

function expressionTypeContextNodes(expression: ts.Expression | undefined): ts.TypeNode[] {
  if (!expression) return [];
  const typeNodes: ts.TypeNode[] = [];
  visit(expression);
  return typeNodes;

  function visit(node: ts.Expression): void {
    let current = node;
    while (true) {
      if (ts.isParenthesizedExpression(current) || ts.isNonNullExpression(current)) {
        current = current.expression;
        continue;
      }
      if (ts.isAsExpression(current) || ts.isTypeAssertionExpression(current)) {
        typeNodes.push(current.type);
        current = current.expression;
        continue;
      }
      if (ts.isSatisfiesExpression(current)) {
        typeNodes.push(current.type);
        current = current.expression;
        continue;
      }
      break;
    }

    if (isObjectFreezeCall(current)) {
      for (const argument of current.arguments) {
        visit(argument);
      }
    }
  }
}

function collectImplementedMemberContracts(
  refs: Reference[],
  seen: Set<string>,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
  repo: string,
  limit: number,
): void {
  if (!ts.isMethodDeclaration(target.node)) return;
  const methodName = propertyNameText(target.node.name);
  if (!methodName) return;

  for (const member of implementedMembers(checker, target)) {
    if (refs.length >= limit) return;
    for (const declaration of member.declarations ?? []) {
      if (refs.length >= limit) return;
      if (isDeclarationInsideTarget(declaration, target.node, target.node.getSourceFile())) continue;
      const source = declaration.getSourceFile();
      if (source.isDeclarationFile || !isInsideRepo(repo, source.fileName)) continue;
      const file = normalizeRelPath(path.relative(repo, source.fileName));
      if (!isRepoRelativePath(file)) continue;
      addReference(refs, seen, referenceForNode(repo, source, declaration, "contract"), limit);
    }
  }
}

function implementedMemberSymbols(checker: ts.TypeChecker, target: CollectedSymbol): Set<ts.Symbol> {
  const symbols = new Set<ts.Symbol>();
  for (const member of implementedMembers(checker, target)) {
    const memberSymbol = canonicalSymbol(checker, member);
    if (memberSymbol) symbols.add(memberSymbol);
    for (const declaration of member.declarations ?? []) {
      const name = declarationName(declaration);
      if (!name) continue;
      const declarationSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(name));
      if (declarationSymbol) symbols.add(declarationSymbol);
    }
  }
  return symbols;
}

function implementedMembers(checker: ts.TypeChecker, target: CollectedSymbol): ts.Symbol[] {
  if (!ts.isMethodDeclaration(target.node)) return [];
  const methodName = propertyNameText(target.node.name);
  if (!methodName) return [];
  const parent = target.node.parent;
  if (!ts.isClassDeclaration(parent)) return [];

  const members: ts.Symbol[] = [];
  for (const clause of parent.heritageClauses ?? []) {
    if (clause.token !== ts.SyntaxKind.ImplementsKeyword && clause.token !== ts.SyntaxKind.ExtendsKeyword) continue;
    for (const heritageType of clause.types) {
      const contractType = checker.getTypeAtLocation(heritageType);
      const member = contractType.getProperty(methodName);
      if (member) members.push(member);
    }
  }
  return members;
}

function declarationName(declaration: ts.Declaration): ts.Node | null {
  return ts.getNameOfDeclaration(declaration) ?? null;
}

function parametersForNode(node: ts.Node): readonly ts.ParameterDeclaration[] {
  if (ts.isClassDeclaration(node)) {
    return constructorParametersForClass(node);
  }
  if (
    ts.isMethodDeclaration(node) ||
    ts.isFunctionDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isArrowFunction(node) ||
    ts.isConstructorDeclaration(node)
  ) {
    return node.parameters;
  }
  return [];
}

function constructorParametersForClass(node: ts.ClassDeclaration): readonly ts.ParameterDeclaration[] {
  const constructor = node.members.find((member): member is ts.ConstructorDeclaration => ts.isConstructorDeclaration(member));
  return constructor?.parameters ?? [];
}

function entityNameLeaf(name: ts.EntityName): ts.Identifier {
  return ts.isQualifiedName(name) ? name.right : name;
}

function schemaReceiverNameNode(expression: ts.Expression): ts.Node | null {
  if (!ts.isPropertyAccessExpression(expression)) return null;
  if (expression.name.text !== "parse" && expression.name.text !== "safeParse") return null;
  const receiver = expression.expression;
  if (ts.isIdentifier(receiver)) return receiver;
  if (ts.isPropertyAccessExpression(receiver)) return receiver.name;
  if (ts.isCallExpression(receiver)) return calleeNameNode(receiver.expression);
  return null;
}

function schemaArgumentNameNode(expression: ts.Expression): ts.Node | null {
  const unwrapped = unwrapExpression(expression);
  if (!unwrapped) return null;
  if (ts.isIdentifier(unwrapped) && isSchemaLikeName(unwrapped.text)) return unwrapped;
  if (ts.isPropertyAccessExpression(unwrapped) && isSchemaLikeName(unwrapped.name.text)) return unwrapped.name;
  return null;
}

function isSchemaLikeName(name: string): boolean {
  return name.endsWith("Schema");
}

function addContractDeclarationReferences(
  refs: Reference[],
  seen: Set<string>,
  symbol: ts.Symbol | null,
  targetNode: ts.Node,
  repo: string,
  limit: number,
): void {
  if (!symbol) return;
  for (const declaration of contractDeclarationsForSymbol(symbol, targetNode, repo)) {
    if (refs.length >= limit) break;
    addReference(refs, seen, referenceForNode(repo, declaration.getSourceFile(), declaration, "contract"), limit);
  }
}

function addContractSymbolWithDependencies(
  refs: Reference[],
  seen: Set<string>,
  contractSymbolsSeen: Set<ts.Symbol>,
  checker: ts.TypeChecker,
  symbol: ts.Symbol | null,
  targetNode: ts.Node,
  repo: string,
  limit: number,
  depth: number,
): void {
  if (!symbol || refs.length >= limit) return;
  const firstVisit = !contractSymbolsSeen.has(symbol);
  if (firstVisit) {
    contractSymbolsSeen.add(symbol);
  }

  const declarations = contractDeclarationsForSymbol(symbol, targetNode, repo);
  for (const declaration of declarations) {
    if (refs.length >= limit) break;
    addReference(refs, seen, referenceForNode(repo, declaration.getSourceFile(), declaration, "contract"), limit);
  }

  if (!firstVisit || depth >= CONTRACT_DEPENDENCY_DEPTH_LIMIT) return;
  for (const declaration of declarations) {
    collectContractDeclarationDependencies(
      refs,
      seen,
      contractSymbolsSeen,
      checker,
      declaration,
      targetNode,
      repo,
      limit,
      depth + 1,
    );
  }
}

function contractDeclarationsForSymbol(symbol: ts.Symbol, targetNode: ts.Node, repo: string): ts.Declaration[] {
  const declarations: ts.Declaration[] = [];
  for (const declaration of symbol.declarations ?? []) {
    if (isDeclarationInsideTarget(declaration, targetNode, targetNode.getSourceFile())) continue;
    const source = declaration.getSourceFile();
    if (source.isDeclarationFile || !isInsideRepo(repo, source.fileName)) continue;
    const file = normalizeRelPath(path.relative(repo, source.fileName));
    if (!isRepoRelativePath(file)) continue;
    declarations.push(declaration);
  }
  return declarations;
}

function collectContractDeclarationDependencies(
  refs: Reference[],
  seen: Set<string>,
  contractSymbolsSeen: Set<ts.Symbol>,
  checker: ts.TypeChecker,
  declaration: ts.Declaration,
  targetNode: ts.Node,
  repo: string,
  limit: number,
  depth: number,
): void {
  const pickedPropertyNamesBySymbol = pickedPropertyNamesByDependencySymbol(declaration, checker);
  visit(declaration);

  function visit(node: ts.Node): void {
    if (refs.length >= limit) return;
    if (node !== declaration && isContractDependencyTraversalBoundary(node)) return;
    if (ts.isIdentifier(node) && isContractDependencyIdentifier(node)) {
      const dependencySymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(node));
      if (dependencySymbol && !symbolHasDeclarationInside(dependencySymbol, declaration)) {
        addContractSymbolWithDependencies(
          refs,
          seen,
          contractSymbolsSeen,
          checker,
          dependencySymbol,
          targetNode,
          repo,
          limit,
          depth,
        );
        const pickedPropertyNames = pickedPropertyNamesBySymbol.get(dependencySymbol);
        if (pickedPropertyNames) {
          addPickedPropertyContractReferences(
            refs,
            seen,
            checker,
            dependencySymbol,
            pickedPropertyNames,
            targetNode,
            repo,
            limit,
            depth,
            new Set(),
          );
        }
      }
    }
    ts.forEachChild(node, visit);
  }
}

function pickedPropertyNamesByDependencySymbol(
  declaration: ts.Declaration,
  checker: ts.TypeChecker,
): Map<ts.Symbol, Set<string>> {
  const namesBySymbol = new Map<ts.Symbol, Set<string>>();
  visit(declaration);
  return namesBySymbol;

  function visit(node: ts.Node): void {
    if (ts.isTypeReferenceNode(node) && entityNameText(node.typeName) === "Pick" && node.typeArguments?.[0] && node.typeArguments[1]) {
      const baseSymbol = symbolForTypeNode(checker, node.typeArguments[0]);
      if (!baseSymbol) {
        ts.forEachChild(node, visit);
        return;
      }
      const names = namesBySymbol.get(baseSymbol) ?? new Set<string>();
      collectStringLiteralTypeNames(node.typeArguments[1], names);
      namesBySymbol.set(baseSymbol, names);
    }
    ts.forEachChild(node, visit);
  }
}

function symbolForTypeNode(checker: ts.TypeChecker, typeNode: ts.TypeNode): ts.Symbol | null {
  if (ts.isTypeReferenceNode(typeNode)) {
    return canonicalSymbol(checker, checker.getSymbolAtLocation(typeNode.typeName));
  }
  if (ts.isTypeQueryNode(typeNode)) {
    return canonicalSymbol(checker, checker.getSymbolAtLocation(typeNode.exprName));
  }
  const type = checker.getTypeAtLocation(typeNode);
  return canonicalSymbol(checker, type.aliasSymbol ?? type.getSymbol());
}

function collectStringLiteralTypeNames(node: ts.Node, names: Set<string>): void {
  if (ts.isLiteralTypeNode(node) && ts.isStringLiteral(node.literal)) {
    names.add(node.literal.text);
    return;
  }
  ts.forEachChild(node, (child) => collectStringLiteralTypeNames(child, names));
}

function addPickedPropertyContractReferences(
  refs: Reference[],
  seen: Set<string>,
  checker: ts.TypeChecker,
  symbol: ts.Symbol | null,
  propertyNames: Set<string>,
  targetNode: ts.Node,
  repo: string,
  limit: number,
  depth: number,
  visited: Set<ts.Symbol>,
): void {
  if (!symbol || propertyNames.size === 0 || refs.length >= limit || depth > CONTRACT_DEPENDENCY_DEPTH_LIMIT) return;
  if (visited.has(symbol)) return;
  visited.add(symbol);

  const declarations = contractDeclarationsForSymbol(symbol, targetNode, repo);
  for (const declaration of declarations) {
    if (refs.length >= limit) return;
    collectPickedPropertyAssignments(refs, seen, declaration, propertyNames, repo, limit);
  }

  if (depth >= CONTRACT_DEPENDENCY_DEPTH_LIMIT) return;

  for (const declaration of declarations) {
    if (refs.length >= limit) return;
    visitDependencies(declaration, declaration);
  }

  function visitDependencies(node: ts.Node, root: ts.Declaration): void {
    if (refs.length >= limit) return;
    if (node !== root && isContractDependencyTraversalBoundary(node)) return;
    if (ts.isIdentifier(node) && isContractDependencyIdentifier(node)) {
      const dependencySymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(node));
      if (dependencySymbol && !symbolHasDeclarationInside(dependencySymbol, root)) {
        addPickedPropertyContractReferences(
          refs,
          seen,
          checker,
          dependencySymbol,
          propertyNames,
          targetNode,
          repo,
          limit,
          depth + 1,
          visited,
        );
      }
    }
    ts.forEachChild(node, (child) => visitDependencies(child, root));
  }
}

function collectPickedPropertyAssignments(
  refs: Reference[],
  seen: Set<string>,
  declaration: ts.Declaration,
  propertyNames: Set<string>,
  repo: string,
  limit: number,
): void {
  visit(declaration);

  function visit(node: ts.Node): void {
    if (refs.length >= limit) return;
    const name = ts.isPropertyAssignment(node) ? propertyNameText(node.name) : null;
    if (name && propertyNames.has(name)) {
      addReference(refs, seen, referenceForNode(repo, node.getSourceFile(), node, "contract"), limit);
      return;
    }
    ts.forEachChild(node, visit);
  }
}

function isContractDependencyTraversalBoundary(node: ts.Node): boolean {
  return (
    ts.isFunctionDeclaration(node) ||
    ts.isFunctionExpression(node) ||
    ts.isArrowFunction(node) ||
    ts.isMethodDeclaration(node) ||
    ts.isConstructorDeclaration(node) ||
    ts.isGetAccessorDeclaration(node) ||
    ts.isSetAccessorDeclaration(node)
  );
}

function isContractDependencyIdentifier(node: ts.Identifier): boolean {
  if (IGNORED_CONTRACT_DEPENDENCY_NAMES.has(node.text)) return false;
  if (hasAncestor(node, ts.isImportDeclaration) || hasAncestor(node, ts.isImportSpecifier) || hasAncestor(node, ts.isImportClause)) {
    return false;
  }
  const parent = node.parent;
  if (ts.isPropertyAccessExpression(parent) && parent.name === node) return false;
  if (ts.isQualifiedName(parent) && parent.right === node) return false;
  if (ts.isPropertyAssignment(parent) && parent.name === node) return false;
  if (ts.isPropertyDeclaration(parent) && parent.name === node) return false;
  if (ts.isMethodDeclaration(parent) && parent.name === node) return false;
  if (ts.isParameter(parent) && parent.name === node) return false;
  if (ts.isVariableDeclaration(parent) && parent.name === node) return false;
  if (ts.isFunctionDeclaration(parent) && parent.name === node) return false;
  if (ts.isClassDeclaration(parent) && parent.name === node) return false;
  if (ts.isInterfaceDeclaration(parent) && parent.name === node) return false;
  if (ts.isTypeAliasDeclaration(parent) && parent.name === node) return false;
  if (ts.isEnumDeclaration(parent) && parent.name === node) return false;
  if (ts.isEnumMember(parent) && parent.name === node) return false;
  if (ts.isBindingElement(parent) && parent.name === node) return false;
  return true;
}

function symbolHasDeclarationInside(symbol: ts.Symbol, container: ts.Node): boolean {
  for (const declaration of symbol.declarations ?? []) {
    if (isDeclarationInsideTarget(declaration, container, container.getSourceFile())) return true;
  }
  return false;
}

function symbolHasDeclarationContainingNode(symbol: ts.Symbol, node: ts.Node): boolean {
  for (const declaration of symbol.declarations ?? []) {
    if (isNodeInsideTarget(node, declaration, declaration.getSourceFile())) return true;
  }
  return false;
}

function symbolForDeclaration(checker: ts.TypeChecker, declaration: ts.Declaration): ts.Symbol | null {
  const name = declarationName(declaration);
  return name ? canonicalSymbol(checker, checker.getSymbolAtLocation(name)) : null;
}

function enclosingImpactDeclaration(node: ts.Node): ts.Declaration | null {
  let current: ts.Node | undefined = node;
  while (current) {
    if (
      ts.isMethodDeclaration(current) ||
      ts.isFunctionDeclaration(current) ||
      ts.isClassDeclaration(current) ||
      ts.isInterfaceDeclaration(current) ||
      ts.isTypeAliasDeclaration(current) ||
      ts.isEnumDeclaration(current)
    ) {
      return current;
    }
    if (ts.isVariableDeclaration(current) && ts.isIdentifier(current.name)) return current;
    if (
      (ts.isFunctionExpression(current) || ts.isArrowFunction(current)) &&
      ts.isVariableDeclaration(current.parent) &&
      ts.isIdentifier(current.parent.name)
    ) {
      return current.parent;
    }
    if (ts.isSourceFile(current)) return null;
    current = current.parent;
  }
  return null;
}

function isNodeInsideTarget(node: ts.Node, target: ts.Node, targetSource: ts.SourceFile): boolean {
  if (node.getSourceFile() !== targetSource) return false;
  const nodeStart = node.getStart(targetSource);
  return nodeStart >= target.getStart(targetSource) && nodeStart < target.getEnd();
}

function isDeclarationInsideTarget(declaration: ts.Declaration, target: ts.Node, targetSource: ts.SourceFile): boolean {
  if (declaration.getSourceFile() !== targetSource) return false;
  const declarationStart = declaration.getStart(targetSource);
  return declarationStart >= target.getStart(targetSource) && declarationStart < target.getEnd();
}

function collectFrameworkMetadata(target: CollectedSymbol, repo: string, limit: number): Reference[] {
  const refs: Reference[] = [];
  const seen = new Set<string>();
  for (const node of metadataNodesForTarget(target)) {
    for (const decorator of decoratorsForNode(node)) {
      addReference(refs, seen, referenceForNode(repo, decorator.getSourceFile(), decorator, "metadata"), limit);
      if (refs.length >= limit) return refs;
    }
  }
  return refs;
}

function metadataNodesForTarget(target: CollectedSymbol): ts.Node[] {
  const nodes: ts.Node[] = [];
  if (ts.isMethodDeclaration(target.node)) {
    const parent = target.node.parent;
    if (ts.isClassDeclaration(parent)) {
      nodes.push(parent);
    }
    nodes.push(target.node);
    nodes.push(...target.node.parameters);
  } else if (ts.isClassDeclaration(target.node)) {
    nodes.push(target.node);
    nodes.push(...constructorParametersForClass(target.node));
    for (const member of target.node.members) {
      nodes.push(member);
      if (ts.isMethodDeclaration(member) || ts.isConstructorDeclaration(member)) {
        nodes.push(...member.parameters);
      }
    }
  }
  return nodes;
}

function collectWorkspaceImportReferences(repo: string, repoIndex: RepoIndex, target: CollectedSymbol, limit: number): Reference[] {
  if (!target.analysis.exported) return [];

  const targetSource = target.node.getSourceFile();
  const targetFile = path.resolve(targetSource.fileName);
  const targetPackage = findIndexedPackageForFile(repo, repoIndex, targetFile);
  if (!targetPackage) return [];
  const exportedNames = exportedNamesForTarget(repoIndex, target, targetPackage);

  const refs: Reference[] = [];
  const seen = new Set<string>();
  for (const entry of repoIndex.files) {
    if (refs.length >= limit) break;
    if (entry.absPath === targetFile) continue;

    const importedBindings = importedBindingsForTarget(entry, repo, target, targetPackage, exportedNames);
    if (importedBindings.localNames.size === 0 && importedBindings.namespaceLocalNames.size === 0) continue;

    for (const importReference of [
      ...importedBindings.localNames.values(),
      ...importedBindings.namespaceLocalNames.values(),
    ]) {
      addReference(refs, seen, importReference, limit);
    }

    for (const identifier of entry.identifiers) {
      if (refs.length >= limit) break;
      if (isIdentifierMatchedByImportedBindings(identifier, importedBindings)) {
        addReference(refs, seen, identifier.reference, limit);
      }
    }
  }

  return refs;
}

function collectWorkspaceMemberReferences(repo: string, repoIndex: RepoIndex, target: CollectedSymbol, limit: number): Reference[] {
  if (!target.exportContainer) return [];

  const targetSource = target.node.getSourceFile();
  const targetFile = path.resolve(targetSource.fileName);
  const targetPackage = findIndexedPackageForFile(repo, repoIndex, targetFile);
  if (!targetPackage) return [];

  const containerTarget: CollectedSymbol = {
    ...target,
    analysis: {
      ...target.analysis,
      name: target.exportContainer.name,
      exported: true,
    },
    defaultExported: target.exportContainer.defaultExported,
    exportContainer: null,
  };
  const exportedNames = exportedNamesForTarget(repoIndex, containerTarget, targetPackage);
  const refs: Reference[] = [];
  const seen = new Set<string>();

  for (const entry of repoIndex.files) {
    if (refs.length >= limit) break;
    if (entry.absPath === targetFile) continue;

    const importedBindings =
      targetPackage && exportedNames
        ? importedBindingsForTarget(entry, repo, containerTarget, targetPackage, exportedNames)
        : emptyImportedBindings();
    if (importedBindings.localNames.size === 0 && importedBindings.namespaceLocalNames.size === 0) continue;

    for (const identifier of entry.identifiers) {
      if (refs.length >= limit) break;
      if (isMemberReferenceForTarget(identifier, target.analysis.name, entry, importedBindings)) {
        addReference(refs, seen, identifier.reference, limit);
      }
    }
  }

  for (const entry of repoIndex.files) {
    if (refs.length >= limit) break;
    if (entry.absPath === targetFile) continue;

    const importedBindings =
      targetPackage && exportedNames
        ? importedBindingsForTarget(entry, repo, containerTarget, targetPackage, exportedNames)
        : emptyImportedBindings();
    if (importedBindings.localNames.size === 0 && importedBindings.namespaceLocalNames.size === 0) continue;

    for (const identifier of entry.identifiers) {
      if (refs.length >= limit) break;
      if (isIdentifierMatchedByImportedBindings(identifier, importedBindings)) {
        addReference(refs, seen, identifier.reference, limit);
      }
    }

    for (const importReference of [
      ...importedBindings.localNames.values(),
      ...importedBindings.namespaceLocalNames.values(),
    ]) {
      addReference(refs, seen, importReference, limit);
    }
  }

  return refs;
}

function filterInvalidWorkspaceMemberReferences(
  repo: string,
  repoIndex: RepoIndex,
  target: CollectedSymbol,
  references: Reference[],
): Reference[] {
  if (!target.exportContainer && target.analysis.kind !== "method") return references;
  const targetSource = target.node.getSourceFile();
  const targetFile = path.resolve(targetSource.fileName);
  const targetPackage = findIndexedPackageForFile(repo, repoIndex, targetFile);
  const containerTarget = containerTargetForMemberTarget(target);
  const exportedNames = targetPackage ? exportedNamesForTarget(repoIndex, containerTarget, targetPackage) : null;
  const validReceiverTypeNames = memberReceiverTypeNames(repoIndex, target);

  return references.filter((reference) => {
    if (!["call", "read", "write"].includes(reference.kind)) return true;
    const entry = repoIndex.files.find((candidate) => candidate.relPath === reference.file);
    if (!entry) return true;
    const importedBindings =
      targetPackage && exportedNames
        ? importedBindingsForTarget(entry, repo, containerTarget, targetPackage, exportedNames)
        : emptyImportedBindings();
    const indexedIdentifiers = entry.identifiers.filter(
      (identifier) =>
        identifier.name === target.analysis.name &&
        identifier.namespaceQualifier !== null &&
        identifier.reference.line === reference.line &&
        identifier.reference.text === reference.text,
    );
    const identifiers =
      indexedIdentifiers.length > 0
        ? indexedIdentifiers
        : inferredMemberIdentifiers(reference, target.analysis.name);
    if (identifiers.length === 0) return true;
    return identifiers.some((identifier) =>
      memberIdentifierHasValidReceiver(identifier, entry, importedBindings, validReceiverTypeNames, target.analysis.name),
    );
  });
}

function emptyImportedBindings(): ImportedBindingsForTarget {
  return {
    localNames: new Map(),
    namespaceLocalNames: new Map(),
    namespaceExportNames: new Map(),
  };
}

function inferredMemberIdentifiers(reference: Reference, memberName: string): IdentifierIndexEntry[] {
  const pattern = new RegExp(`\\b((?:this|[A-Za-z_$][\\w$]*)(?:\\.[A-Za-z_$][\\w$]*)*)\\.${escapeRegExp(memberName)}\\b`, "g");
  const identifiers: IdentifierIndexEntry[] = [];
  for (const match of reference.text.matchAll(pattern)) {
    identifiers.push({
      name: memberName,
      namespaceQualifier: match[1],
      reference,
    });
  }
  return identifiers;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function memberIdentifierHasValidReceiver(
  identifier: IdentifierIndexEntry,
  entry: RepoFileIndexEntry,
  bindings: ImportedBindingsForTarget,
  validReceiverTypeNames: Set<string>,
  memberName: string,
): boolean {
  if (!identifier.namespaceQualifier) return true;
  if (qualifierMatchesImportedContainer(identifier.namespaceQualifier, bindings, memberName)) return true;

  const receiver = receiverForReference(entry, identifier);
  if (!receiver) return false;
  if (!receiver.typeName) return false;

  const typeNames = new Set([receiver.typeName, ...expandTypeAlias(receiver.typeName, entry, bindings)]);
  for (const typeName of typeNames) {
    if (typeNameMatchesImportedContainer(typeName, bindings) || typeNameSetHas(validReceiverTypeNames, typeName)) {
      return true;
    }
  }
  return false;
}

function memberReceiverTypeNames(repoIndex: RepoIndex, target: CollectedSymbol): Set<string> {
  const names = new Set<string>();
  if (target.exportContainer) names.add(target.exportContainer.name);
  const parent = target.node.parent;
  if (parent && ts.isClassDeclaration(parent)) {
    if (parent.name) names.add(parent.name.text);
    for (const clause of parent.heritageClauses ?? []) {
      for (const heritageType of clause.types) {
        const name = expressionNameText(heritageType.expression);
        if (name) names.add(name);
      }
    }
  }
  expandDerivedTypeNames(repoIndex, names);
  return names;
}

function expandDerivedTypeNames(repoIndex: RepoIndex, names: Set<string>): void {
  let changed = true;
  while (changed) {
    changed = false;
    for (const entry of repoIndex.files) {
      for (const heritage of entry.classHeritages) {
        if (typeNameSetHas(names, heritage.className)) continue;
        if (!heritage.baseNames.some((baseName) => typeNameSetHas(names, baseName))) continue;
        names.add(heritage.className);
        changed = true;
      }
    }
  }
}

function typeNameSetHas(typeNames: Set<string>, candidate: string): boolean {
  if (typeNames.has(candidate)) return true;
  const simple = simpleTypeName(candidate);
  return simple !== candidate && typeNames.has(simple);
}

function simpleTypeName(typeName: string): string {
  const dotIndex = typeName.lastIndexOf(".");
  return dotIndex === -1 ? typeName : typeName.slice(dotIndex + 1);
}

function collectWorkspaceDiReferences(repo: string, repoIndex: RepoIndex, target: CollectedSymbol, limit: number): Reference[] {
  const diTarget = diTargetForSymbol(target);
  if (!diTarget.analysis.exported) return [];

  const targetSource = diTarget.node.getSourceFile();
  const targetFile = path.resolve(targetSource.fileName);
  const targetPackage = findIndexedPackageForFile(repo, repoIndex, targetFile);
  if (!targetPackage) return [];

  const exportedNames = exportedNamesForTarget(repoIndex, diTarget, targetPackage);
  const refs: Reference[] = [];
  const seen = new Set<string>();
  const tokenNames = new Set<string>();
  const providerArrayNames = new Set<string>();

  for (const entry of repoIndex.files) {
    if (refs.length >= limit) break;
    if (entry.absPath === targetFile) continue;

    const importedBindings = importedBindingsForTarget(entry, repo, diTarget, targetPackage, exportedNames);
    if (importedBindings.localNames.size === 0 && importedBindings.namespaceLocalNames.size === 0) continue;

    for (const provider of entry.diProviders) {
      if (refs.length >= limit) break;
      if (!importedBindings.localNames.has(provider.implementationName)) continue;
      tokenNames.add(provider.tokenName);
      if (provider.sourceArrayName) {
        providerArrayNames.add(provider.sourceArrayName);
      }
      addReference(refs, seen, provider.reference, limit);
    }
  }

  if (providerArrayNames.size > 0) {
    for (const entry of repoIndex.files) {
      if (refs.length >= limit) break;
      for (const provider of entry.diProviders) {
        if (refs.length >= limit) break;
        if (!providerArrayNames.has(provider.implementationName)) continue;
        addReference(refs, seen, provider.reference, limit);
      }
    }
  }

  if (tokenNames.size === 0) return refs;

  for (const entry of repoIndex.files) {
    if (refs.length >= limit) break;
    for (const injection of entry.diInjections) {
      if (!tokenNames.has(injection.tokenName)) continue;
      addReference(refs, seen, injection.reference, limit);
    }
  }

  return refs;
}

function collectProviderTokenInjectionReferences(
  repo: string,
  repoIndex: RepoIndex,
  target: CollectedSymbol,
  limit: number,
): Reference[] {
  const providerObject = diProviderObjectForNode(target.node);
  const provideProperty = providerObject ? propertyAssignmentNamed(providerObject, "provide") : null;
  const token = provideProperty ? identifierFromExpression(provideProperty.initializer) : null;
  if (!token) return [];

  const refs: Reference[] = [];
  const seen = new Set<string>();
  for (const entry of repoIndex.files) {
    if (refs.length >= limit) break;
    for (const injection of entry.diInjections) {
      if (refs.length >= limit) break;
      if (injection.tokenName !== token.text) continue;
      addReference(refs, seen, injection.reference, limit);
    }
  }
  return refs;
}

function diProviderObjectForNode(node: ts.Node): ts.ObjectLiteralExpression | null {
  let current: ts.Node | undefined = node;
  while (current) {
    if (ts.isObjectLiteralExpression(current) && propertyAssignmentNamed(current, "provide")) {
      return current;
    }
    if (ts.isSourceFile(current)) return null;
    current = current.parent;
  }
  return null;
}

function diTargetForSymbol(target: CollectedSymbol): CollectedSymbol {
  return containerTargetForMemberTarget(target);
}

function containerTargetForMemberTarget(target: CollectedSymbol): CollectedSymbol {
  if (!target.exportContainer) return target;
  return {
    ...target,
    analysis: {
      ...target.analysis,
      name: target.exportContainer.name,
      exported: true,
    },
    defaultExported: target.exportContainer.defaultExported,
    exportContainer: null,
  };
}

function isMemberReferenceForTarget(
  identifier: IdentifierIndexEntry,
  memberName: string,
  entry: RepoFileIndexEntry,
  bindings: ImportedBindingsForTarget,
): boolean {
  if (identifier.name !== memberName || identifier.namespaceQualifier === null || identifier.reference.kind === "import") {
    return false;
  }
  if (qualifierMatchesImportedContainer(identifier.namespaceQualifier, bindings, memberName)) {
    return true;
  }
  const typeNames = typedReceiverTypesForReference(entry, bindings, identifier);
  for (const typeName of typeNames) {
    if (typeNameMatchesImportedContainer(typeName, bindings)) return true;
  }
  return false;
}

function typedReceiverTypesForReference(
  entry: RepoFileIndexEntry,
  bindings: ImportedBindingsForTarget,
  identifier: IdentifierIndexEntry,
): Set<string> {
  const typeNames = new Set<string>();
  const receiver = receiverForReference(entry, identifier);
  if (!receiver?.typeName) return typeNames;
  typeNames.add(receiver.typeName);
  for (const expanded of expandTypeAlias(receiver.typeName, entry, bindings)) {
    typeNames.add(expanded);
  }
  return typeNames;
}

function receiverForReference(entry: RepoFileIndexEntry, identifier: IdentifierIndexEntry): ReceiverIndexEntry | null {
  const position = identifier.reference.line;
  return (
    entry.receivers
      .filter(
        (receiver) =>
          receiver.receiverName === identifier.namespaceQualifier &&
          receiver.startLine <= position &&
          position <= receiver.endLine,
      )
      .sort((left, right) => right.startLine - left.startLine || left.endLine - right.endLine)[0] ?? null
  );
}

function typeNameMatchesImportedContainer(typeName: string, bindings: ImportedBindingsForTarget): boolean {
  if (bindings.localNames.has(typeName)) return true;
  return qualifiedNameMatchesImportedContainer(typeName, bindings);
}

function expandTypeAlias(typeName: string, entry: RepoFileIndexEntry, bindings: ImportedBindingsForTarget): Set<string> {
  const resolved = new Set<string>();
  const queue = [typeName];
  const seen = new Set<string>();
  while (queue.length > 0) {
    const current = queue.shift()!;
    if (seen.has(current)) continue;
    seen.add(current);
    const alias = entry.typeAliases.find((candidate) => candidate.name === current);
    if (!alias) continue;
    resolved.add(alias.targetName);
    if (!typeNameMatchesImportedContainer(alias.targetName, bindings)) {
      queue.push(alias.targetName);
    }
  }
  return resolved;
}

function qualifierMatchesImportedContainer(
  qualifier: string,
  bindings: ImportedBindingsForTarget,
  memberName: string,
): boolean {
  if (bindings.localNames.has(qualifier)) return true;
  if (bindings.namespaceExportNames.get(qualifier)?.has(memberName)) return true;
  return qualifiedNameMatchesImportedContainer(qualifier, bindings);
}

function qualifiedNameMatchesImportedContainer(value: string, bindings: ImportedBindingsForTarget): boolean {
  const dotIndex = value.lastIndexOf(".");
  if (dotIndex === -1) return false;
  const namespaceName = value.slice(0, dotIndex);
  const exportedName = value.slice(dotIndex + 1);
  return bindings.namespaceExportNames.get(namespaceName)?.has(exportedName) ?? false;
}

function exportedNamesForTarget(repoIndex: RepoIndex, target: CollectedSymbol, targetPackage: PackageInfo): ExportedNamesForTarget {
  const targetPath = path.resolve(target.node.getSourceFile().fileName);
  const allNames = new Set<string>();
  const byFile = new Map<string, Set<string>>();
  const namespacesByFile = new Map<string, Map<string, Set<string>>>();
  const queue = [{ filePath: targetPath, exportName: target.analysis.name }];
  if (target.defaultExported) {
    queue.push({ filePath: targetPath, exportName: "default" });
  }

  const seen = new Set<string>();
  for (let index = 0; index < queue.length; index += 1) {
    const current = queue[index];
    const key = `${current.filePath}:${current.exportName}`;
    if (seen.has(key)) continue;
    seen.add(key);
    allNames.add(current.exportName);
    addExportedNameForFile(byFile, current.filePath, current.exportName);

    for (const entry of repoIndex.files) {
      for (const exportEntry of entry.exports) {
        if (exportEntry.localName === STAR_EXPORT_LOCAL_NAME) {
          if (current.exportName === "default") continue;
          if (!isExportEntryRelatedToPath(exportEntry, entry.absPath, current.filePath, targetPackage)) continue;
          queue.push({ filePath: path.resolve(entry.absPath), exportName: current.exportName });
          continue;
        }
        if (exportEntry.localName === NAMESPACE_EXPORT_LOCAL_NAME) {
          if (!isExportEntryRelatedToPath(exportEntry, entry.absPath, current.filePath, targetPackage)) continue;
          addNamespaceExportedNameForFile(namespacesByFile, entry.absPath, exportEntry.exportedName, current.exportName);
          continue;
        }
        if (exportEntry.localName !== current.exportName) continue;
        if (!isExportEntryRelatedToPath(exportEntry, entry.absPath, current.filePath, targetPackage)) continue;
        queue.push({ filePath: path.resolve(entry.absPath), exportName: exportEntry.exportedName });
      }
    }
  }

  return { allNames, byFile, namespacesByFile };
}

function addExportedNameForFile(namesByFile: Map<string, Set<string>>, filePath: string, name: string): void {
  const key = normalizeRelPath(path.resolve(filePath));
  const names = namesByFile.get(key) ?? new Set<string>();
  names.add(name);
  namesByFile.set(key, names);
}

function addNamespaceExportedNameForFile(
  namespacesByFile: Map<string, Map<string, Set<string>>>,
  filePath: string,
  namespaceName: string,
  memberName: string,
): void {
  const key = normalizeRelPath(path.resolve(filePath));
  const namespaces = namespacesByFile.get(key) ?? new Map<string, Set<string>>();
  const memberNames = namespaces.get(namespaceName) ?? new Set<string>();
  memberNames.add(memberName);
  namespaces.set(namespaceName, memberNames);
  namespacesByFile.set(key, namespaces);
}

function isExportEntryRelatedToPath(
  exportEntry: ExportIndexEntry,
  exporterPath: string,
  sourcePath: string,
  targetPackage: PackageInfo,
): boolean {
  if (exportEntry.moduleSpecifier === null) {
    return path.resolve(exporterPath) === path.resolve(sourcePath);
  }
  return isModuleSpecifierRelatedToPath(exportEntry.moduleSpecifier, exporterPath, sourcePath, targetPackage);
}

function importedBindingsForTarget(
  entry: RepoFileIndexEntry,
  repo: string,
  target: CollectedSymbol,
  targetPackage: PackageInfo,
  exportedNames: ExportedNamesForTarget,
): ImportedBindingsForTarget {
  const bindings = {
    localNames: new Map<string, Reference>(),
    namespaceLocalNames: new Map<string, Reference>(),
    namespaceExportNames: new Map<string, Set<string>>(),
  };
  for (const importEntry of entry.imports) {
    const importExportNames = exportedNamesForImport(importEntry.moduleSpecifier, entry.absPath, repo, targetPackage, exportedNames);
    const importNamespaceExportNames = exportedNamespaceNamesForImport(
      importEntry.moduleSpecifier,
      entry.absPath,
      repo,
      targetPackage,
      exportedNames,
    );
    if (
      isModuleSpecifierRelatedToPath(importEntry.moduleSpecifier, entry.absPath, target.node.getSourceFile().fileName, targetPackage)
    ) {
      for (const name of exportedNames.allNames) {
        importExportNames.add(name);
      }
    }
    if (importExportNames.size === 0 && importNamespaceExportNames.size === 0) {
      continue;
    }

    if (importEntry.defaultImport && importExportNames.has("default")) {
      bindings.localNames.set(importEntry.defaultImport.localName, importEntry.defaultImport.reference);
    }

    if (importEntry.namespaceImport) {
      bindings.namespaceLocalNames.set(importEntry.namespaceImport.localName, importEntry.namespaceImport.reference);
      bindings.namespaceExportNames.set(importEntry.namespaceImport.localName, importExportNames);
      for (const [namespaceName, memberNames] of importNamespaceExportNames.entries()) {
        const localNamespaceName = `${importEntry.namespaceImport.localName}.${namespaceName}`;
        bindings.namespaceLocalNames.set(localNamespaceName, importEntry.namespaceImport.reference);
        bindings.namespaceExportNames.set(localNamespaceName, memberNames);
      }
    }

    for (const namedImport of importEntry.namedImports) {
      const namespaceMemberNames = importNamespaceExportNames.get(namedImport.importedName);
      if (namespaceMemberNames) {
        bindings.namespaceLocalNames.set(namedImport.localName, namedImport.reference);
        bindings.namespaceExportNames.set(namedImport.localName, namespaceMemberNames);
        continue;
      }
      if (importExportNames.has(namedImport.importedName)) {
        bindings.localNames.set(namedImport.localName, namedImport.reference);
      }
    }
  }
  return bindings;
}

function exportedNamesForImport(
  specifier: string,
  importerPath: string,
  repo: string,
  targetPackage: PackageInfo,
  exportedNames: ExportedNamesForTarget,
): Set<string> {
  const names = new Set<string>();
  for (const candidate of moduleSpecifierCandidatePaths(specifier, importerPath, repo, targetPackage)) {
    const candidateNames = exportedNames.byFile.get(candidate);
    if (!candidateNames) continue;
    for (const name of candidateNames) {
      names.add(name);
    }
  }
  return names;
}

function exportedNamespaceNamesForImport(
  specifier: string,
  importerPath: string,
  repo: string,
  targetPackage: PackageInfo,
  exportedNames: ExportedNamesForTarget,
): Map<string, Set<string>> {
  const namespaces = new Map<string, Set<string>>();
  for (const candidate of moduleSpecifierCandidatePaths(specifier, importerPath, repo, targetPackage)) {
    const candidateNamespaces = exportedNames.namespacesByFile.get(candidate);
    if (!candidateNamespaces) continue;
    for (const [namespaceName, memberNames] of candidateNamespaces.entries()) {
      const names = namespaces.get(namespaceName) ?? new Set<string>();
      for (const memberName of memberNames) {
        names.add(memberName);
      }
      namespaces.set(namespaceName, names);
    }
  }
  return namespaces;
}

function isIdentifierMatchedByImportedBindings(
  identifier: IdentifierIndexEntry,
  bindings: ImportedBindingsForTarget,
): boolean {
  if (bindings.localNames.has(identifier.name)) return true;
  if (identifier.namespaceQualifier === null || !bindings.namespaceLocalNames.has(identifier.namespaceQualifier)) {
    return false;
  }
  return bindings.namespaceExportNames.get(identifier.namespaceQualifier)?.has(identifier.name) ?? false;
}

function isReferenceToTarget(node: ts.Identifier, checker: ts.TypeChecker, target: CollectedSymbol): boolean {
  if (target.tsSymbol) {
    const nodeSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(node));
    if (nodeSymbol !== target.tsSymbol) return false;
    if (target.exportContainer && isPropertyAccessMemberName(node)) {
      return memberReceiverMatchesExportContainer(node, checker, target);
    }
    return true;
  }
  return node.text === target.analysis.name;
}

function isPropertyAccessMemberName(node: ts.Identifier): boolean {
  return ts.isPropertyAccessExpression(node.parent) && node.parent.name === node;
}

function memberReceiverMatchesExportContainer(
  node: ts.Identifier,
  checker: ts.TypeChecker,
  target: CollectedSymbol,
): boolean {
  const parent = node.parent;
  if (!ts.isPropertyAccessExpression(parent) || parent.name !== node) return true;
  const receiverType = checker.getTypeAtLocation(parent.expression);
  return typeMatchesTargetContainer(receiverType, checker, target);
}

function typeMatchesTargetContainer(type: ts.Type, checker: ts.TypeChecker, target: CollectedSymbol): boolean {
  const container = target.containerNode ?? target.node.parent;
  if (!container) return false;
  const apparentType = checker.getApparentType(type);
  const candidates = [type.getSymbol(), type.aliasSymbol, apparentType.getSymbol(), apparentType.aliasSymbol];
  return candidates.some((symbol) => symbolMatchesOrExtendsNode(symbol, container, checker));
}

function symbolHasDeclarationMatchingNode(symbol: ts.Symbol | undefined, node: ts.Node): boolean {
  if (!symbol) return false;
  return (symbol.declarations ?? []).some((declaration) => isSameNodeRange(declaration, node));
}

function symbolMatchesOrExtendsNode(symbol: ts.Symbol | undefined, node: ts.Node, checker: ts.TypeChecker): boolean {
  if (!symbol) return false;
  return (symbol.declarations ?? []).some((declaration) =>
    declarationMatchesOrExtendsNode(declaration, node, checker, new Set()),
  );
}

function declarationMatchesOrExtendsNode(
  declaration: ts.Declaration,
  node: ts.Node,
  checker: ts.TypeChecker,
  seen: Set<string>,
): boolean {
  if (isSameNodeRange(declaration, node)) return true;
  if (!isHeritageDeclaration(declaration)) return false;

  const key = `${declaration.getSourceFile().fileName}:${declaration.getStart(declaration.getSourceFile())}:${declaration.getEnd()}`;
  if (seen.has(key)) return false;
  seen.add(key);

  for (const clause of declaration.heritageClauses ?? []) {
    for (const heritageType of clause.types) {
      const heritageSymbol = canonicalSymbol(checker, checker.getSymbolAtLocation(heritageType.expression));
      if (!heritageSymbol) continue;
      for (const heritageDeclaration of heritageSymbol.declarations ?? []) {
        if (declarationMatchesOrExtendsNode(heritageDeclaration, node, checker, seen)) return true;
      }
    }
  }
  return false;
}

function isHeritageDeclaration(node: ts.Node): node is ts.ClassDeclaration | ts.InterfaceDeclaration {
  return ts.isClassDeclaration(node) || ts.isInterfaceDeclaration(node);
}

function isSameNodeRange(left: ts.Node, right: ts.Node): boolean {
  return (
    left.getSourceFile().fileName === right.getSourceFile().fileName &&
    left.getStart(left.getSourceFile()) === right.getStart(right.getSourceFile()) &&
    left.getEnd() === right.getEnd()
  );
}
