import { WebhookRoutingService } from '../webhook-routing.service.js';

it('uses webhook routing during retrigger', () => {
  expect(new WebhookRoutingService().buildJobs('applicantReviewed')).toEqual(['identity-kyc', 'identity-pep']);
});
