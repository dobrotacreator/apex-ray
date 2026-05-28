export const CoreBankPersonSchema = {
  parse: (value: unknown): { id: string; verificationStatus: number } =>
    value as { id: string; verificationStatus: number },
  shape: {
    id: 'string',
    verificationStatus: 'number',
  },
};
