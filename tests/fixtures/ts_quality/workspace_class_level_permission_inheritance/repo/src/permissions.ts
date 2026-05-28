export const Permission = {
  QUOTE_RECONCILE_MANAGE: 'QUOTE_RECONCILE_MANAGE',
} as const;

export type Permission = (typeof Permission)[keyof typeof Permission];
