import { describe, expect, it } from 'vitest';
import { ActorType, auditEntrySchema, buildAuditEvent, newAuditEventSchema } from './audit-event.js';

describe('buildAuditEvent', () => {
  it('normalizes optional fields to storage-friendly defaults', () => {
    const event = buildAuditEvent({
      entityType: 'ORDER',
      entityId: 'order-1',
      action: 'QUOTE_CREATED',
    });

    expect(event).toEqual({
      entityType: 'ORDER',
      entityId: 'order-1',
      action: 'QUOTE_CREATED',
      actorId: null,
      actorType: ActorType.SYSTEM,
      previousState: null,
      newState: null,
      metadata: {},
    });
    expect(newAuditEventSchema.parse(event)).toEqual(event);
  });

  it('preserves explicit actor, states, metadata, and idempotency key', () => {
    const event = buildAuditEvent({
      entityType: 'QUOTE',
      entityId: 'quote-1',
      action: 'QUOTE_ACCEPTED',
      actorId: 'client-1',
      actorType: ActorType.CLIENT,
      previousState: 'PUBLISHED',
      newState: 'ACCEPTED',
      metadata: { order_id: 'order-1' },
      idempotencyKey: 'audit:quote-1:QUOTE_ACCEPTED',
    });

    expect(event).toMatchObject({
      actorId: 'client-1',
      actorType: ActorType.CLIENT,
      previousState: 'PUBLISHED',
      newState: 'ACCEPTED',
      metadata: { order_id: 'order-1' },
      idempotencyKey: 'audit:quote-1:QUOTE_ACCEPTED',
    });
  });

  it('rejects empty identifiers and unknown top-level fields', () => {
    expect(() =>
      auditEntrySchema.parse({
        entityType: '',
        entityId: 'request-1',
        action: 'GATEWAY_REQUEST_RECEIVED',
      }),
    ).toThrow();
  });
});
