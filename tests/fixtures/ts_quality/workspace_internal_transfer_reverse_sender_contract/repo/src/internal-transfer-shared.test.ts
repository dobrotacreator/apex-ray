import { reverseInternalTransfer } from './internal-transfer-shared.js';
import type { ProviderTransferPort, ProviderTransferRequest } from './provider.js';
import type { InternalTransferPayload } from './step-payloads.js';

describe('reverseInternalTransfer', () => {
  it('uses the client sender owner when reversing a forward transfer that targets the client account', async () => {
    const requests: ProviderTransferRequest[] = [];
    const provider: ProviderTransferPort = {
      async createTransfer(request) {
        requests.push(request);
      },
    };
    const payload: InternalTransferPayload = {
      sourceAccountId: 'fce-account',
      destinationAccountId: 'client-account',
      targetsClientAccount: true,
      providerAccountOwnerRef: 'owner:provider',
      clientOwnerRef: 'owner:client',
    };

    await reverseInternalTransfer(provider, payload, 'reverse-idempotency-key');

    expect(requests[0]).toMatchObject({
      fromAccountId: 'client-account',
      toAccountId: 'fce-account',
      senderOwnerRef: 'owner:client',
      idempotencyKey: 'reverse-idempotency-key',
    });
  });
});
