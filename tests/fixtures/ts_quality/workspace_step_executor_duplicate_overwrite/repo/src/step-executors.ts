import { StepType } from './step-type.js';
import type { StepExecutorPort } from './step-executor.port.js';

export class InternalVaultTransferExecutor implements StepExecutorPort {
  readonly stepType = StepType.INTERNAL_VAULT_TRANSFER;

  async dispatch(stepId: string): Promise<string> {
    return `vault:${stepId}`;
  }

  async reverse(stepId: string): Promise<string> {
    return `vault-reverse:${stepId}`;
  }
}

export class InternalBankTransferExecutor implements StepExecutorPort {
  readonly stepType = StepType.INTERNAL_BANK_TRANSFER;

  async dispatch(stepId: string): Promise<string> {
    return `bank:${stepId}`;
  }

  async reverse(stepId: string): Promise<string> {
    return `bank-reverse:${stepId}`;
  }
}
