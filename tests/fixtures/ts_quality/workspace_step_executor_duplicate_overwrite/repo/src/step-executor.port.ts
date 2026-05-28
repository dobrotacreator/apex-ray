import type { StepType } from './step-type.js';

export const STEP_EXECUTOR_PORT = Symbol('STEP_EXECUTOR_PORT');

export interface StepExecutorPort {
  readonly stepType: StepType;
  dispatch(stepId: string): Promise<string>;
  reverse(stepId: string): Promise<string>;
}
