import { BusinessRuleError } from './errors.js';
export class WebhookRetriggerAlreadyInProgressError extends BusinessRuleError {
  static readonly wireDetailKeys = [] as const;
  constructor(public readonly retryAfterMs: number) {
    super('Webhook retrigger is already in progress');
    this.detail = { retryAfterMs };
  }
}
