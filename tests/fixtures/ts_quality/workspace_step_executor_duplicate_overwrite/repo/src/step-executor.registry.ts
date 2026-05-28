import { Inject, Injectable } from './testing-nest.js';
import type { StepType } from './step-type.js';
import { STEP_EXECUTOR_PORT, type StepExecutorPort } from './step-executor.port.js';

@Injectable()
export class StepExecutorRegistry {
  private readonly byStepType: Map<StepType, StepExecutorPort>;

  constructor(@Inject(STEP_EXECUTOR_PORT) executors: ReadonlyArray<StepExecutorPort>) {
    const m = new Map<StepType, StepExecutorPort>();
    for (const executor of executors) {
      m.set(executor.stepType, executor);
    }
    this.byStepType = m;
  }

  resolve(stepType: StepType): StepExecutorPort | null {
    return this.byStepType.get(stepType) ?? null;
  }

  get registeredStepTypes(): ReadonlySet<StepType> {
    return new Set(this.byStepType.keys());
  }
}
