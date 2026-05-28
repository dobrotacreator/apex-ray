import { QuoteStepPayloadSchema, STEP_TYPES } from './step-payloads.js';
export function extractQuoteStepType(raw: unknown): (typeof STEP_TYPES)[number] {
  const parsed = QuoteStepPayloadSchema.parse(raw);
  return parsed.settlementId as (typeof STEP_TYPES)[number];
}
