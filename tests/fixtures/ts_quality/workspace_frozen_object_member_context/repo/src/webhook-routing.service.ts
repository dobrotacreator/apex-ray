import { PROXY_TARGET_ENV } from './webhook-routing.js';

export function envForTarget(target: 'identity-kyc' | 'vault'): string {
  return PROXY_TARGET_ENV[target] ?? PROXY_TARGET_ENV['identity-kyc'];
}
