import { expect, it } from 'vitest';
import { matchBankRoute } from './bank-routes.js';

it('keeps GET account lookup unsigned', () => {
  expect(matchBankRoute('GET', '/v2/accounts/acc-1')).toEqual({ signed: false });
});
