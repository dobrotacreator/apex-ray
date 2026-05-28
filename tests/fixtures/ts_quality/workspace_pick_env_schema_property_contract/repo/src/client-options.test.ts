import { buildCoreBankHttpClientOptions } from './client-options.js';

describe('buildCoreBankHttpClientOptions', () => {
  it('preserves env defaults when CoreBank URL is omitted', () => {
    const options = buildCoreBankHttpClientOptions({
      COREBANK_API_KEY: undefined,
      COREBANK_TIMEOUT_MS: 15000,
      COREBANK_RESPONSE_BODY_LOG_CHARS: 4096,
      COREBANK_REQUEST_BODY_LOG_CHARS: 512,
    });

    expect(options.baseUrl).toBe('');
    expect(options.timeoutMs).toBe(15000);
    expect(options.responseBodyLogChars).toBe(4096);
    expect(options.requestBodyLogChars).toBe(512);
  });
});
