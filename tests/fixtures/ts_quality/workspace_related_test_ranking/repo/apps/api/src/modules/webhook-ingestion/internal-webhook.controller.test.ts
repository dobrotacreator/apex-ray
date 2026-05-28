import { WebhookRoutingService } from './webhook-routing.service.js';

it('enqueues proxy jobs from routing', () => {
  expect(new WebhookRoutingService().buildJobs('applicantReviewed')).toEqual(['identity-kyc', 'identity-pep']);
});
