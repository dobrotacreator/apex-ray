import { StepType, ValidationError } from './types.js';

export { ALL_STEP_TYPES, StepType } from './types.js';

export const ACTIVE_STEP_TYPES: readonly StepType[] = [
  StepType.INTERNAL_VAULT_TRANSFER,
  StepType.INTERNAL_BANK_TRANSFER,
  StepType.OPERATOR_CONFIRMATION,
  StepType.LAYER_2_SCREENING,
  StepType.EXTERNAL_CRYPTO_SEND,
];

export const REVERSIBLE_STEP_TYPES: ReadonlySet<StepType> = new Set([
  StepType.INTERNAL_VAULT_TRANSFER,
  StepType.INTERNAL_BANK_TRANSFER,
  StepType.OPERATOR_CONFIRMATION,
  StepType.LAYER_2_SCREENING,
  StepType.LEDGER_POSTING,
]);

export const TRANSFER_STEP_TYPES: ReadonlySet<StepType> = new Set([
  StepType.INTERNAL_VAULT_TRANSFER,
  StepType.INTERNAL_BANK_TRANSFER,
  StepType.EXTERNAL_CRYPTO_SEND,
  StepType.EXTERNAL_FIAT_SEND,
  StepType.EXTERNAL_TRAVEL_RULE_EXCHANGE,
]);

const STEP_TYPE_VALUES = Object.values(StepType);

export function parseStepType(value: string): StepType {
  const match = STEP_TYPE_VALUES.find((v) => v === value);
  if (match === undefined) {
    throw new ValidationError(`Invalid step type: ${value}`);
  }
  return match;
}

export function assertActiveIsSubsetOfReversible(): void {
  const violations: StepType[] = [];
  for (const stepType of ACTIVE_STEP_TYPES) {
    if (!REVERSIBLE_STEP_TYPES.has(stepType)) {
      violations.push(stepType);
    }
  }
  if (violations.length > 0) {
    throw new Error(
      `Boot invariant violated: ACTIVE_STEP_TYPES contains types not in REVERSIBLE_STEP_TYPES: ${violations.join(', ')}. ` +
        'Every active step type must declare a reverse() implementation in its executor.',
    );
  }
}
