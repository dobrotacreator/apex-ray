import { QuoteStepPayloadSchema } from './step-payloads.js';
export function mapQuotePayload(raw: unknown): string {
  const parsed = QuoteStepPayloadSchema.parse(raw);
  return parsed.settlementId;
}
