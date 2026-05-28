export const Permission = {
  QUOTE_BROKER_VIEW: 'QUOTE_BROKER_VIEW',
  QUOTE_BROKER_MANAGE: 'QUOTE_BROKER_MANAGE',
} as const;

export type Permission = (typeof Permission)[keyof typeof Permission];
