export interface WebhookInboxDetail {
  id: string;
  providerHeaders: Record<string, string> | null;
}

export interface ApiResponse<T> {
  data: T;
}
