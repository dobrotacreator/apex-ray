export interface PublicInternalTransferPayloadBase {
  sourceAccountId: string;
  destinationAccountId: string;
  targetsClientAccount: boolean;
  // Optional only for legacy/read DTOs; execution payloads require this owner ref.
  providerAccountOwnerRef?: string;
}
