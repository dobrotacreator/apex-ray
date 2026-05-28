import { expect, it } from 'vitest';
import { targetsFor } from './webhook-routing.js';

it('fans out applicantReviewed to KYC and PEP', () => {
  expect(targetsFor('applicantReviewed')).toEqual(['identity-kyc', 'identity-pep']);
});
