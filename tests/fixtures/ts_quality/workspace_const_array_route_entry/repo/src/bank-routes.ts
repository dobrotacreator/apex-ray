interface BankRouteDefinition {
  method: string;
  template: string;
  signed: boolean;
  pattern: RegExp;
}

const BANK_ROUTES: BankRouteDefinition[] = [
  { method: 'POST', template: '/v2/accounts', signed: true, pattern: /^\/v2\/accounts$/u },
  {
    method: 'GET',
    template: '/v2/accounts/{accountIdentifier}',
    signed: true,
    pattern: /^\/v2\/accounts\/[^/]+$/u,
  },
];

export function matchBankRoute(method: string, path: string): { signed: boolean } | undefined {
  const normalizedMethod = method.toUpperCase();
  const matched = BANK_ROUTES.find(
    (definition) => definition.method === normalizedMethod && definition.pattern.test(path),
  );
  return matched === undefined ? undefined : { signed: matched.signed };
}
