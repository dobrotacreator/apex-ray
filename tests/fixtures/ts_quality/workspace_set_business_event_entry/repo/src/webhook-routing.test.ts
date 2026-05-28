import { expect, it } from 'vitest';
import { isBusinessEvent } from './webhook-routing.js';

it('treats applicantReviewed as business event', () => {
  expect(isBusinessEvent('applicantReviewed')).toBe(true);
});
