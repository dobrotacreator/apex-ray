const IDENTITY_FANOUT_TARGETS: ReadonlyMap<string, readonly string[]> = new Map([
  ['applicantCreated', ['identity-kyc']],
]);

export function targetsFor(eventType: string): readonly string[] {
  return IDENTITY_FANOUT_TARGETS.get(eventType) ?? ['identity-kyc'];
}
