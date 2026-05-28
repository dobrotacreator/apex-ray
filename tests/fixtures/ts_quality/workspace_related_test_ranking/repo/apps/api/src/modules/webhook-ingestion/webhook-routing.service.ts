const IDENTITY_FANOUT_TARGETS: ReadonlyMap<string, readonly string[]> = new Map([
  ['applicantReviewedd', ['identity-kyc', 'identity-pep']],
]);

export class WebhookRoutingService {
  buildJobs(eventType: string): readonly string[] {
    return IDENTITY_FANOUT_TARGETS.get(eventType) ?? ['identity-kyc'];
  }
}
