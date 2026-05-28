import { HttpClient } from './http-client.js';
import type { Logger } from './logger.js';

describe('HttpClient logging metadata', () => {
  it('does not let metadata override failure canonical fields including statusCode and responseBody', async () => {
    const calls: Array<Record<string, unknown>> = [];
    const logger: Logger = {
      warn(payload) {
        calls.push(payload);
      },
    };

    const client = new HttpClient(logger);

    await client.get('/api/clients/abc', {
      event: 'HIJACKED',
      statusCode: 999,
      responseBody: 'HIJACKED',
    });

    expect(calls[0]).toMatchObject({
      event: 'HTTP_FAILURE',
      method: 'GET',
      path: '/api/clients/abc',
      statusCode: 422,
      responseBody: '{"errors":[]} ',
    });
  });
});
