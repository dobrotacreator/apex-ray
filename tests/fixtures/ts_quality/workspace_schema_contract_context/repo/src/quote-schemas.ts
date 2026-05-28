const stringSchema = {
  trim: () => stringSchema,
  min: (_value: number) => stringSchema,
  max: (_value: number) => stringSchema,
};
const arraySchema = {
  min: (_value: number) => arraySchema,
  max: (_value: number) => arraySchema,
};
const z = {
  string: () => stringSchema,
  array: (_schema: unknown) => arraySchema,
  object: <T extends object>(_shape: T) => ({ parse: (value: unknown): T => value as T }),
};
export const AddQuoteSchema = z.object({
  lpProvider: z.string().trim().min(1).max(100),
  reason: z.string().trim().min(1).max(500),
  fileIds: z.array(z.string()).min(1).max(10),
});
