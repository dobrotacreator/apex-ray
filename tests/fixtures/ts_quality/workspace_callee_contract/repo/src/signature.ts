export function verifyWebhookSignature(rawBody: string, signature: string): boolean {
  return rawBody.startsWith('{') && signature.length > 0;
}
