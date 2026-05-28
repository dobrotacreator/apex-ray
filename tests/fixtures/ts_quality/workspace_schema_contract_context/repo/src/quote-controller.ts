import { AddQuoteSchema } from './quote-schemas.js';
export function addQuote(body: unknown): { reason: unknown; fileCount: number } {
  const parsed = AddQuoteSchema.parse(body);
  return { reason: parsed.reason, fileCount: 0 };
}
