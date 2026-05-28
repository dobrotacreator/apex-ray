import { Inject, Injectable } from "@nestjs/common";
import type { SettlementStep } from "@acme/settlement";
import { STEP_EXECUTOR_PORT, type StepExecutorPort } from "./step-executor.port";

@Injectable()
export class StepExecutorRegistry {
  constructor(@Inject(STEP_EXECUTOR_PORT) private readonly executors: ReadonlyArray<StepExecutorPort>) {}

  getExecutor(step: SettlementStep): StepExecutorPort | undefined {
    return this.executors.find((executor) => executor.supports(step));
  }
}
