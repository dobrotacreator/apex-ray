export interface DomainEvent {
  readonly type: 'RECONCILED';
  readonly orderId: string;
}

export interface DomainQueue {
  /**
   * Publishes the domain event after the database transaction commits.
   * Callers must not enqueue this from inside UnitOfWork.transaction,
   * because rollback would leave a phantom side effect in the queue.
   */
  publish(event: DomainEvent): Promise<void>;
}
