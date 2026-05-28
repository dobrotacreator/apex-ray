import { z } from 'zod';
import { auditIdempotencyKeySchema } from './idempotency.js';

export const ActorType = {
  CLIENT: 'CLIENT',
  OPERATOR: 'OPERATOR',
  SYSTEM: 'SYSTEM',
} as const;
export type ActorType = (typeof ActorType)[keyof typeof ActorType];

export interface AuditEntry<
  ActorId extends string = string,
  EntityType extends string = string,
  Action extends string = string,
> {
  entityType: EntityType;
  entityId: string;
  action: Action;
  actorId?: ActorId | null;
  actorType?: ActorType;
  previousState?: string | null;
  newState?: string | null;
  metadata?: Record<string, unknown>;
  idempotencyKey?: string;
}

const auditStringWhitespaceMessage = 'Audit string fields must not contain leading or trailing whitespace.';

function isAuditString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0 && value.trim() === value;
}

const auditStringSchema = z
  .string()
  .min(1)
  .refine((value) => value.trim() === value, {
    message: auditStringWhitespaceMessage,
  });

export const auditActorTypeSchema = z.enum(ActorType);
export const auditMetadataSchema = z.record(z.string(), z.unknown());

const typedAuditStringSchema = <Value extends string>() =>
  z.custom<Value>((value) => isAuditString(value), {
    message: auditStringWhitespaceMessage,
  });

const typedAuditEntrySchema = <ActorId extends string, EntityType extends string, Action extends string>() =>
  z
    .object({
      entityType: typedAuditStringSchema<EntityType>(),
      entityId: auditStringSchema,
      action: typedAuditStringSchema<Action>(),
      actorId: typedAuditStringSchema<ActorId>().nullable().optional(),
      actorType: auditActorTypeSchema.optional(),
      previousState: auditStringSchema.nullable().optional(),
      newState: auditStringSchema.nullable().optional(),
      metadata: auditMetadataSchema.optional(),
      idempotencyKey: auditIdempotencyKeySchema.optional(),
    })
    .strict();

export const auditEntrySchema = z
  .object({
    entityType: auditStringSchema,
    entityId: auditStringSchema,
    action: auditStringSchema,
    actorId: auditStringSchema.nullable().optional(),
    actorType: auditActorTypeSchema.optional(),
    previousState: auditStringSchema.nullable().optional(),
    newState: auditStringSchema.nullable().optional(),
    metadata: auditMetadataSchema.optional(),
    idempotencyKey: auditIdempotencyKeySchema.optional(),
  })
  .strict();

export const newAuditEventSchema = z
  .object({
    entityType: auditStringSchema,
    entityId: auditStringSchema,
    action: auditStringSchema,
    actorId: auditStringSchema.nullable(),
    actorType: auditActorTypeSchema,
    previousState: auditStringSchema.nullable(),
    newState: auditStringSchema.nullable(),
    metadata: auditMetadataSchema,
    idempotencyKey: auditIdempotencyKeySchema.optional(),
  })
  .strict();

export interface NewAuditEvent<
  ActorId extends string = string,
  EntityType extends string = string,
  Action extends string = string,
> {
  entityType: EntityType;
  entityId: string;
  action: Action;
  actorId: ActorId | null;
  actorType: ActorType;
  previousState: string | null;
  newState: string | null;
  metadata: Record<string, unknown>;
  idempotencyKey?: string;
}

export interface AuditEvent<
  ActorId extends string = string,
  EntityType extends string = string,
  Action extends string = string,
> extends NewAuditEvent<ActorId, EntityType, Action> {
  id: string;
  createdAt: Date;
}

export function buildAuditEvent<
  ActorId extends string = string,
  EntityType extends string = string,
  Action extends string = string,
>(entry: AuditEntry<ActorId, EntityType, Action>): NewAuditEvent<ActorId, EntityType, Action> {
  const parsed = typedAuditEntrySchema<ActorId, EntityType, Action>().parse(entry);

  return {
    entityType: parsed.entityType,
    entityId: parsed.entityId,
    action: parsed.action,
    actorId: parsed.actorId ?? null,
    actorType: parsed.actorType ?? ActorType.OPERATOR,
    previousState: parsed.previousState ?? null,
    newState: parsed.newState ?? null,
    metadata: parsed.metadata ?? {},
    idempotencyKey: parsed.idempotencyKey,
  };
}
