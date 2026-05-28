import { expect, it } from 'vitest';
import { targetsFor } from './webhook-routing.js';

it('routes reviewed KYT transactions to travel-rule fanout', () => {
  expect(targetsFor('applicantKytTxnReviewed')).toEqual(['identity-kyc', 'identity-travel-rule']);
});
