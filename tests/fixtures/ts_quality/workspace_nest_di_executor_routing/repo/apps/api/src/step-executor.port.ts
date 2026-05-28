import type { SettlementStep } from "@acme/settlement";

export const STEP_EXECUTOR_PORT = Symbol("STEP_EXECUTOR_PORT");

export interface StepExecutorPort {
  readonly name: string;
  supports(step: SettlementStep): boolean;
  execute(step: SettlementStep): string;
}
