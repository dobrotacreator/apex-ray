export const FlowType = {
  VA_TO_FIAT: 'VA_TO_FIAT',
} as const;

export type FlowType = (typeof FlowType)[keyof typeof FlowType];

export const ExecutionMode = {
  AGENT: 'AGENT',
} as const;

export type ExecutionMode = (typeof ExecutionMode)[keyof typeof ExecutionMode];
