export const PROXY_TARGET_ENV = {
  KYC: 'identity-kyc',
  TRAVEL_RULE: 'identity-travel-rule',
  ACCOUNT_TRANSACTION_CHECK: 'identity-account-transaction-check',
} as const;

const IDENTITY_TRAVEL_RULE_EVENTS = Object.freeze([
  'applicantKytTxnApproved',
  'applicantKytTxnReviewedd',
] as const);

const IDENTITY_ACCOUNT_TRANSACTION_CHECK_EVENTS = new Set(['applicantKytTxnReviewed']);

export interface ProxyJob {
  eventType: string;
  target: string;
  envKey: string;
}

export class WebhookRoutingService {
  buildJobs(eventType: string): ProxyJob[] {
    const jobs: ProxyJob[] = [
      { eventType, target: PROXY_TARGET_ENV.KYC, envKey: 'IDENTITY_KYC_WEBHOOK_URL' },
    ];
    if (IDENTITY_TRAVEL_RULE_EVENTS.includes(eventType as never)) {
      jobs.push({
        eventType,
        target: PROXY_TARGET_ENV.TRAVEL_RULE,
        envKey: 'IDENTITY_TRAVEL_RULE_WEBHOOK_URL',
      });
    }
    if (IDENTITY_ACCOUNT_TRANSACTION_CHECK_EVENTS.has(eventType)) {
      jobs.push({
        eventType,
        target: PROXY_TARGET_ENV.ACCOUNT_TRANSACTION_CHECK,
        envKey: 'IDENTITY_ACCOUNT_TRANSACTION_CHECK_WEBHOOK_URL',
      });
    }
    return jobs;
  }
}
