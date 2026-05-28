import type { DomainQueue } from './queue.port.js';
import type { UnitOfWork } from './unit-of-work.port.js';
export class ReconcileService {
  constructor(private readonly uow: UnitOfWork, private readonly queue: DomainQueue) {}
  async reconcile(orderId: string): Promise<void> {
    const postCommitEmit = await this.uow.transaction(async (trx) => {
      await trx.save(orderId);
      await this.queue.publish({ type: 'RECONCILED', orderId });
      return () => Promise.resolve();
    });
    await postCommitEmit();
  }
}
