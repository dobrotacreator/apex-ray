import type { PublicInternalTransferPayloadBase } from './public-quote.js';

export interface InternalTransferPayload extends PublicInternalTransferPayloadBase {
  providerAccountOwnerRef: string;
  clientOwnerRef: string;
}
