export interface TransactionHandle {
  save(orderId: string): Promise<void>;
}

export interface UnitOfWork {
  /**
   * Runs the callback inside one database transaction.
   * Return post-commit work to the caller and execute it only after this
   * promise resolves; side effects inside the callback can survive rollback.
   */
  transaction<T>(callback: (trx: TransactionHandle) => Promise<T>): Promise<T>;
}
