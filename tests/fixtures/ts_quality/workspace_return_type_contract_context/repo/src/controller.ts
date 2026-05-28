import type { ApiResponse, WebhookInboxDetail } from './response.js';

export function detail(id: string): ApiResponse<WebhookInboxDetail> {
  return { data: { id, providerHeaders: null } };
}
