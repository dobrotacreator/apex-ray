import { randomUUID } from 'node:crypto';
import {
  ActorType,
  type AuditEvent,
  buildAuditEvent,
  type NewAuditEvent,
} from '@apex-fixture/audit-core/audit-event';
import { buildAuditEventIdempotencyKey } from '@apex-fixture/audit-core/idempotency';

type GatewayAuditEntityType = 'GATEWAY_REQUEST';
type GatewayAuditEventCode = 'REQUEST_RECEIVED' | 'REQUEST_FAILED';
type GatewayAuditMetadata = Record<string, unknown>;

interface GatewayAuditRecordInput {
  requestId: string;
  eventCode: GatewayAuditEventCode;
  callerId: string;
}

export type GatewayAuditEvent = AuditEvent<string, GatewayAuditEntityType, GatewayAuditEventCode>;
export type GatewayNewAuditEvent = NewAuditEvent<string, GatewayAuditEntityType, GatewayAuditEventCode>;

export class GatewayAuditService {
  async record(input: GatewayAuditRecordInput): Promise<GatewayAuditEvent> {
    const idempotencyKey = buildAuditEventIdempotencyKey({
      scope: 'operations-gateway',
      entityType: 'GATEWAY_REQUEST',
      entityId: input.requestId,
      action: input.eventCode,
    });

    const event = {
      id: randomUUID(),
      createdAt: new Date(),
      ...buildAuditEvent<string, GatewayAuditEntityType, GatewayAuditEventCode>({
        entityType: 'GATEWAY_REQUEST',
        entityId: input.requestId,
        action: input.eventCode,
        actorId: input.callerId === 'unknown' ? null : input.callerId,
        actorType: input.callerId === 'unknown' ? ActorType.SYSTEM : ActorType.CLIENT,
        previousState: null,
        newState: null,
        metadata: {} satisfies GatewayAuditMetadata,
        idempotencyKey,
      }),
    } satisfies GatewayAuditEvent;

    return event;
  }
}
