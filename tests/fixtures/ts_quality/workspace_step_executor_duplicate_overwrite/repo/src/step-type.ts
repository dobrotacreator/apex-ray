export const StepType = {
  INTERNAL_VAULT_TRANSFER: 'INTERNAL_VAULT_TRANSFER',
  INTERNAL_BANK_TRANSFER: 'INTERNAL_BANK_TRANSFER',
} as const;

export type StepType = (typeof StepType)[keyof typeof StepType];
