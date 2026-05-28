const IDENTITY_TRAVEL_RULE_EVENTS = Object.freeze([
  'applicantKytTxnApproved',
  'applicantKytTxnReviewedd',
] as const);

const IDENTITY_FANOUT_TARGETS: ReadonlyMap<string, readonly string[]> = new Map([
  ...IDENTITY_TRAVEL_RULE_EVENTS.map((eventType): [string, readonly string[]] => [
    eventType,
    ['identity-kyc', 'identity-travel-rule'],
  ]),
]);

export function targetsFor(eventType: string): readonly string[] {
  return IDENTITY_FANOUT_TARGETS.get(eventType) ?? ['identity-kyc'];
}
