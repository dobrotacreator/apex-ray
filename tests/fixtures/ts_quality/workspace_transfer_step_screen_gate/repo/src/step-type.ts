import { StepType } from './types.js';

export { StepType } from './types.js';

export const ACTIVE_STEP_TYPES: readonly StepType[] = [
  StepType.INTERNAL_VAULT_TRANSFER,
  StepType.INTERNAL_BANK_TRANSFER,
  StepType.OPERATOR_CONFIRMATION,
  StepType.LAYER_2_SCREENING,
];

export const REVERSIBLE_STEP_TYPES: ReadonlySet<StepType> = new Set([
  StepType.INTERNAL_VAULT_TRANSFER,
  StepType.INTERNAL_BANK_TRANSFER,
  StepType.OPERATOR_CONFIRMATION,
  StepType.LAYER_2_SCREENING,
]);

export const TRANSFER_STEP_TYPES: ReadonlySet<StepType> = new Set([
  StepType.INTERNAL_VAULT_TRANSFER,
  StepType.EXTERNAL_CRYPTO_SEND,
  StepType.EXTERNAL_FIAT_SEND,
  StepType.EXTERNAL_TRAVEL_RULE_EXCHANGE,
]);
