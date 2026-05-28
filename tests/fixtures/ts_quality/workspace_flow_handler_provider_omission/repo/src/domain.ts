export const FlowType = {
  VA_TO_FIAT: 'VA_TO_FIAT',
  VA_TO_VA: 'VA_TO_VA',
} as const;

export type FlowType = (typeof FlowType)[keyof typeof FlowType];

export const ExecutionMode = {
  AGENT: 'AGENT',
} as const;

export type ExecutionMode = (typeof ExecutionMode)[keyof typeof ExecutionMode];

export type HandlerVersion = string;

export function handlerVersion(value: string): HandlerVersion {
  return value;
}
