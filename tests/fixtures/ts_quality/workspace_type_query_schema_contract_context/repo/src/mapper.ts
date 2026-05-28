import type { Infer } from './schemas.js';
import { CoreBankEmailSchema } from './schemas.js';

export function extractPrimaryEmail(emails: Infer<typeof CoreBankEmailSchema>[] | undefined): string {
  const first = emails?.[0];
  return first?.email ?? '';
}
