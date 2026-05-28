import { verifyWebhookSignature } from './signature.js';
export function handleWebhook(body: unknown, signature: string): boolean {
  return verifyWebhookSignature(JSON.stringify(body), signature);
}
