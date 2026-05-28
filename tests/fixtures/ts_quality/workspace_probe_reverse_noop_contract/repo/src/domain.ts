export const StepType = {
  INTERNAL_VAULT_TRANSFER: 'INTERNAL_VAULT_TRANSFER',
  LAYER_2_SCREENING: 'LAYER_2_SCREENING',
} as const;

export type StepType = (typeof StepType)[keyof typeof StepType];

export interface Order {
  id: string;
}

export interface SettlementStep {
  id: string;
  stepType: StepType;
  externalId: string | null;
}
