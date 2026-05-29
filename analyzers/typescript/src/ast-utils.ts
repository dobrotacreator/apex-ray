import ts from "typescript";

export function propertyNameText(name: ts.PropertyName): string | null {
  if (ts.isIdentifier(name) || ts.isStringLiteral(name) || ts.isNumericLiteral(name)) return name.text;
  return null;
}

export function nodeLineRange(source: ts.SourceFile, node: ts.Node): { startLine: number; endLine: number } {
  const startLine = source.getLineAndCharacterOfPosition(node.getFullStart()).line + 1;
  const endLine = source.getLineAndCharacterOfPosition(node.getEnd()).line + 1;
  return { startLine, endLine };
}

export function expressionNameText(expression: ts.Expression): string | null {
  if (ts.isIdentifier(expression)) return expression.text;
  if (ts.isPropertyAccessExpression(expression)) {
    const qualifier = expressionNameText(expression.expression);
    return qualifier ? `${qualifier}.${expression.name.text}` : null;
  }
  return null;
}

export function calleeNameNode(expression: ts.Expression): ts.Node | null {
  if (ts.isIdentifier(expression)) return expression;
  if (ts.isPropertyAccessExpression(expression)) return expression.name;
  return null;
}

export function propertyAssignmentNamed(
  object: ts.ObjectLiteralExpression,
  name: string,
): ts.PropertyAssignment | null {
  for (const property of object.properties) {
    if (!ts.isPropertyAssignment(property)) continue;
    if (propertyNameText(property.name) === name) return property;
  }
  return null;
}

export function identifierFromExpression(expression: ts.Expression | undefined): ts.Identifier | null {
  const unwrapped = unwrapExpression(expression);
  return unwrapped && ts.isIdentifier(unwrapped) ? unwrapped : null;
}

export function identifiersFromArrayExpression(expression: ts.Expression | undefined): ts.Identifier[] {
  const unwrapped = unwrapExpression(expression);
  if (!unwrapped || !ts.isArrayLiteralExpression(unwrapped)) return [];
  return unwrapped.elements
    .map((element) => (ts.isSpreadElement(element) ? identifierFromExpression(element.expression) : identifierFromExpression(element)))
    .filter((identifier): identifier is ts.Identifier => identifier !== null);
}

export function arrayLiteralExpressionForInitializer(expression: ts.Expression | undefined): ts.ArrayLiteralExpression | null {
  const unwrapped = unwrapExpression(expression);
  if (!unwrapped) return null;
  if (ts.isArrayLiteralExpression(unwrapped)) return unwrapped;
  if (!isObjectFreezeCall(unwrapped) && !isCollectionConstructorCall(unwrapped)) return null;

  const [argument] = unwrapped.arguments ?? [];
  const arrayArgument = unwrapExpression(argument);
  return arrayArgument && ts.isArrayLiteralExpression(arrayArgument) ? arrayArgument : null;
}

export function isObjectFreezeCall(expression: ts.Expression): expression is ts.CallExpression {
  if (!ts.isCallExpression(expression)) return false;
  const callee = expression.expression;
  return (
    ts.isPropertyAccessExpression(callee) &&
    ts.isIdentifier(callee.expression) &&
    callee.expression.text === "Object" &&
    callee.name.text === "freeze"
  );
}

function isCollectionConstructorCall(expression: ts.Expression): expression is ts.NewExpression {
  if (!ts.isNewExpression(expression)) return false;
  if (!ts.isIdentifier(expression.expression)) return false;
  return expression.expression.text === "Map" || expression.expression.text === "Set";
}

export function unwrapExpression(expression: ts.Expression | undefined): ts.Expression | null {
  if (!expression) return null;
  let current = expression;
  while (true) {
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

export function entityNameText(name: ts.EntityName): string {
  return ts.isIdentifier(name) ? name.text : `${entityNameText(name.left)}.${name.right.text}`;
}

export function decoratorsForNode(node: ts.Node): readonly ts.Decorator[] {
  if (!ts.canHaveDecorators(node)) return [];
  return ts.getDecorators(node) ?? [];
}

export function moduleExportNameText(name: ts.ModuleExportName): string {
  return name.text;
}
