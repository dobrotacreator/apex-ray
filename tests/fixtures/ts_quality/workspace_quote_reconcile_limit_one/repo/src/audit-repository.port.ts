export interface AuditRepository {
  record(action: string, entityId: string, metadata?: Record<string, unknown>): Promise<void>;
}
