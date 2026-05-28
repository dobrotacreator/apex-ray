export interface AuditEvent {
  id: string;
  tenantId: string;
  action: string;
}

export function buildAuditEvent(tenantId: string, action: string): AuditEvent {
  return {
    id: action,
    tenantId,
    action,
  };
}
