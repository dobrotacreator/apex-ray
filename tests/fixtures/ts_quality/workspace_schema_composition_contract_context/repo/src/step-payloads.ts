export const STEP_TYPES = ['QUOTE_REQUESTED', 'QUOTE_PRICED'] as const;

export const MoneySchema = {
  parse: (value: unknown): { amount: string; currency: 'USD' | 'EUR' } =>
    value as { amount: string; currency: 'USD' | 'EUR' },
};

export const BaseStepPayloadSchema = {
  parse: (value: unknown): { stepType: (typeof STEP_TYPES)[number]; settlementId: string } =>
    value as { stepType: (typeof STEP_TYPES)[number]; settlementId: string },
  stepTypes: STEP_TYPES,
};

export const QuoteStepPayloadSchema = {
  parse: (value: unknown): { stepType: (typeof STEP_TYPES)[number]; settlementId: string; money: ReturnType<typeof MoneySchema.parse> } =>
    value as { stepType: (typeof STEP_TYPES)[number]; settlementId: string; money: ReturnType<typeof MoneySchema.parse> },
  base: BaseStepPayloadSchema,
  money: MoneySchema,
};
