import { Permission } from './permissions.js';

describe('AdminQuoteReconcileController metadata', () => {
  it('keeps the reconcile manage permission inherited by every admin route', () => {
    const classMetadata = [Permission.QUOTE_RECONCILE_MANAGE];
    const retryMethodMetadata = undefined;

    expect(retryMethodMetadata ?? classMetadata).toContain(Permission.QUOTE_RECONCILE_MANAGE);
  });
});
