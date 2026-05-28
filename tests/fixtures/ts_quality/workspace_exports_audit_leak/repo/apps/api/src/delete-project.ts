import { buildAuditEvent } from "@acme/audit/audit-event";

export function createDeleteProjectAuditEvent(tenantId: string, projectId: string) {
  return buildAuditEvent(tenantId, `delete-project:${projectId}`);
}
