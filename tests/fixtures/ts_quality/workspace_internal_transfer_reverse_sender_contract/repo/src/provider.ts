export interface ProviderTransferRequest {
  fromAccountId: string;
  toAccountId: string;
  senderOwnerRef: string;
  idempotencyKey: string;
}

export interface ProviderTransferPort {
  createTransfer(request: ProviderTransferRequest): Promise<void>;
}
