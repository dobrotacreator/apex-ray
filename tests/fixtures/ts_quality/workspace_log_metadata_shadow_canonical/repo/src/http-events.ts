export const HttpEvent = {
  REQUEST: 'HTTP_REQUEST',
  RESPONSE: 'HTTP_RESPONSE',
  FAILURE: 'HTTP_FAILURE',
} as const;

export type HttpEvent = (typeof HttpEvent)[keyof typeof HttpEvent];
