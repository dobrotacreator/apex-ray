const IDENTITY_BUSINESS_EVENTS = new Set([
  'applicantReviewedd',
]);

export function isBusinessEvent(eventType: string): boolean {
  return IDENTITY_BUSINESS_EVENTS.has(eventType) || eventType.startsWith('applicantAction');
}
