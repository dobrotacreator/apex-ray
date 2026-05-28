export type Infer<T> = T extends { parse(value: unknown): infer Output } ? Output : never;

export const CoreBankEmailSchema = {
  parse: (value: unknown): { email: string; primary?: boolean } => value as { email: string; primary?: boolean },
  shape: {
    email: 'string',
    primary: 'boolean?',
  },
};
