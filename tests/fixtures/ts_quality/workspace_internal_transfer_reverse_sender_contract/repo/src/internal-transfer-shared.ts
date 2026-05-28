import type { ProviderTransferPort } from './provider.js';
import type { InternalTransferPayload } from './step-payloads.js';

export function forwardSenderOwnerRef(payload: InternalTransferPayload): string {
  return payload.targetsClientAccount ? payload.providerAccountOwnerRef : payload.clientOwnerRef;
}

export function reverseSenderOwnerRef(payload: InternalTransferPayload): string {
  return payload.targetsClientAccount ? payload.providerAccountOwnerRef : payload.clientOwnerRef;
}

export async function dispatchInternalTransfer(
  provider: ProviderTransferPort,
  payload: InternalTransferPayload,
  idempotencyKey: string,
): Promise<void> {
  await provider.createTransfer({
    fromAccountId: payload.sourceAccountId,
    toAccountId: payload.destinationAccountId,
    senderOwnerRef: forwardSenderOwnerRef(payload),
    idempotencyKey,
  });
}

export async function reverseInternalTransfer(
  provider: ProviderTransferPort,
  payload: InternalTransferPayload,
  reverseIdempotencyKey: string,
): Promise<void> {
  await provider.createTransfer({
    fromAccountId: payload.destinationAccountId,
    toAccountId: payload.sourceAccountId,
    senderOwnerRef: reverseSenderOwnerRef(payload),
    idempotencyKey: reverseIdempotencyKey,
  });
}
