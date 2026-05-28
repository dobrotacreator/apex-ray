import { Module } from './testing-nest.js';
import { STEP_EXECUTOR_PORT } from './step-executor.port.js';
import { StepExecutorRegistry } from './step-executor.registry.js';
import { InternalBankTransferExecutor, InternalVaultTransferExecutor } from './step-executors.js';

@Module({
  providers: [
    InternalVaultTransferExecutor,
    InternalBankTransferExecutor,
    {
      provide: STEP_EXECUTOR_PORT,
      useFactory: (vault: InternalVaultTransferExecutor, bank: InternalBankTransferExecutor) => [vault, bank],
      inject: [InternalVaultTransferExecutor, InternalBankTransferExecutor],
    },
    StepExecutorRegistry,
  ],
  exports: [StepExecutorRegistry],
})
export class SettlementModule {}
