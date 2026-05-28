import type { Env } from './env.js';

export const PROXY_TARGET_ENV = Object.freeze({
  'identity-kyc': 'COREBANK_IDENTITY_TRAVEL_RULE_WEBHOOK_URL',
} as const) satisfies Readonly<Record<string, keyof Env>>;
