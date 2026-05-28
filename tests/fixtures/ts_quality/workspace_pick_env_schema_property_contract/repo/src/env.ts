type Infer<T> = T extends { output: infer Output } ? Output : never;

export const envSchema = {
  output: {} as {
    STORAGE_BUCKET: string;
    COREBANK_API_URL?: string;
    COREBANK_API_KEY?: string;
    COREBANK_TIMEOUT_MS?: number;
    COREBANK_RESPONSE_BODY_LOG_CHARS: number;
    COREBANK_REQUEST_BODY_LOG_CHARS?: number;
  },
  shape: {
    STORAGE_BUCKET: 'string',
    COREBANK_API_URL: 'url?',
    COREBANK_API_KEY: 'string?',
    COREBANK_TIMEOUT_MS: 'number?',
    COREBANK_RESPONSE_BODY_LOG_CHARS: 'number default 4096',
    COREBANK_REQUEST_BODY_LOG_CHARS: 'number?',
  },
};

export const validatedEnvSchema = envSchema;
export type Env = Infer<typeof validatedEnvSchema>;
