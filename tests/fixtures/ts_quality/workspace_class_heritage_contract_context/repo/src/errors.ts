export abstract class DomainError extends Error {
  /** Keys copied to the public wire error detail payload. */
  static readonly wireDetailKeys?: readonly string[];
  detail?: Record<string, unknown>;
}

export abstract class BusinessRuleError extends DomainError {}
