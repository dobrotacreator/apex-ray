import type { Env } from './env.js';

interface CoreBankHttpClientOptions {
  readonly baseUrl: string;
  readonly apiKey: string;
  readonly timeoutMs?: number;
  readonly responseBodyLogChars?: number;
  readonly requestBodyLogChars?: number;
}

interface CoreBankConfigPort {
  readonly apiUrl: string | undefined;
  readonly apiKey: string | undefined;
  readonly timeoutMs?: number;
  readonly responseBodyLogChars?: number;
  readonly requestBodyLogChars?: number;
}

type CoreBankHttpClientEnv = Pick<
  Env,
  | 'COREBANK_API_URL'
  | 'COREBANK_API_KEY'
  | 'COREBANK_TIMEOUT_MS'
  | 'COREBANK_RESPONSE_BODY_LOG_CHARS'
  | 'COREBANK_REQUEST_BODY_LOG_CHARS'
>;

type CoreBankHttpClientConfig = Pick<
  CoreBankConfigPort,
  'apiUrl' | 'apiKey' | 'timeoutMs' | 'responseBodyLogChars' | 'requestBodyLogChars'
>;

export function buildCoreBankHttpClientOptions(
  env: CoreBankHttpClientEnv,
): CoreBankHttpClientOptions;
export function buildCoreBankHttpClientOptions(
  config: CoreBankHttpClientConfig,
): CoreBankHttpClientOptions;
export function buildCoreBankHttpClientOptions(
  source: CoreBankHttpClientEnv | CoreBankHttpClientConfig,
): CoreBankHttpClientOptions {
  const defaults = isEnvSource(source)
    ? {
        baseUrl: source.COREBANK_API_URL ?? '',
        apiKey: source.COREBANK_API_KEY ?? '',
        timeoutMs: source.COREBANK_TIMEOUT_MS,
        responseBodyLogChars: source.COREBANK_RESPONSE_BODY_LOG_CHARS,
        requestBodyLogChars: source.COREBANK_REQUEST_BODY_LOG_CHARS,
      }
    : {
        baseUrl: source.apiUrl ?? '',
        apiKey: source.apiKey ?? '',
        timeoutMs: source.timeoutMs,
        responseBodyLogChars: source.responseBodyLogChars,
        requestBodyLogChars: source.requestBodyLogChars,
      };
  return defaults;
}

function isEnvSource(source: CoreBankHttpClientEnv | CoreBankHttpClientConfig): source is CoreBankHttpClientEnv {
  return 'COREBANK_API_URL' in source;
}
