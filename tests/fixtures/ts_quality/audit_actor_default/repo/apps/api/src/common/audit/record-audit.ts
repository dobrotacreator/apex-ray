import { buildAuditEvent as buildCoreAuditEvent } from '@apex-fixture/audit-core/audit-event';
import type { AuditEntry } from './audit-catalog.js';
import type { NewAuditEvent } from './audit-event.js';

/**
 * Build a `NewAuditEvent` from a concise `AuditEntry`.
 * Defaults: actorType = 'SYSTEM', actorId = null, metadata = {}.
 */
export function buildAuditEvent(entry: AuditEntry): NewAuditEvent {
  return buildCoreAuditEvent(entry);
}
