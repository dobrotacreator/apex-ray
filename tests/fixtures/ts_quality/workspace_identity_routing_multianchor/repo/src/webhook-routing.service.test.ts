import { AdminWebhookOpsService } from './admin-webhook-ops.service.js';
import { WebhookRoutingService } from './webhook-routing.service.js';

describe('Identity webhook routing', () => {
  it('fans reviewed KYT transactions out to travel-rule and account-check targets', () => {
    const jobs = new WebhookRoutingService().buildJobs('applicantKytTxnReviewed');
    expect(jobs.map((job) => job.target)).toEqual([
      'identity-kyc',
      'identity-travel-rule',
      'identity-account-transaction-check',
    ]);
  });

  it('uses routing output as admin expected delivery targets', () => {
    const service = new AdminWebhookOpsService();
    expect(service.expectedTargetsFor('applicantKytTxnReviewed')).toEqual([
      'identity-kyc',
      'identity-travel-rule',
      'identity-account-transaction-check',
    ]);
  });
});
