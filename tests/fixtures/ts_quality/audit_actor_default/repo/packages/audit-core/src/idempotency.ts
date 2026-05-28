import { z } from 'zod';

const MAX_AUDIT_IDEMPOTENCY_KEY_LENGTH = 512;
export const AUDIT_IDEMPOTENCY_KEY_SEPARATOR = ':';

export const auditIdempotencySegmentSchema = z.union([
  z.string().min(1),
  z.number(),
  z.boolean(),
  z.date(),
]);
export type AuditIdempotencySegment = z.input<typeof auditIdempotencySegmentSchema>;

export const auditIdempotencyKeySchema = z.string().min(1).max(MAX_AUDIT_IDEMPOTENCY_KEY_LENGTH);
export type AuditIdempotencyKey = z.infer<typeof auditIdempotencyKeySchema>;

export interface AuditEventIdempotencyKeyParts {
  scope?: AuditIdempotencySegment;
  entityType: AuditIdempotencySegment;
  entityId: AuditIdempotencySegment;
  action: AuditIdempotencySegment;
  discriminator?: AuditIdempotencySegment | readonly AuditIdempotencySegment[];
}

export function buildAuditEventIdempotencyKey(parts: AuditEventIdempotencyKeyParts): AuditIdempotencyKey {
  return auditIdempotencyKeySchema.parse(
    [parts.scope ?? 'audit', parts.entityType, parts.entityId, parts.action].join(AUDIT_IDEMPOTENCY_KEY_SEPARATOR),
  );
}
