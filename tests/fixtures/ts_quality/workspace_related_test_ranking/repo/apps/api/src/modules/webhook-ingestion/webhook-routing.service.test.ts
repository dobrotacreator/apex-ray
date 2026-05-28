import { WebhookRoutingService } from './webhook-routing.service.js';

it('applicantReviewed fans out to KYC and PEP', () => {
  expect(new WebhookRoutingService().buildJobs('applicantReviewed')).toEqual(['identity-kyc', 'identity-pep']);
});
