export interface Logger {
  warn(payload: Record<string, unknown>, message: string): void;
}
