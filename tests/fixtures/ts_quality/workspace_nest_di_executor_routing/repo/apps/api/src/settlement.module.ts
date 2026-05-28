import { Module } from "@nestjs/common";
import { InternalBankTransferExecutor, InternalVaultTransferExecutor } from "@acme/settlement";
import { STEP_EXECUTOR_PORT } from "./step-executor.port";
import { StepExecutorRegistry } from "./step-executor.registry";

@Module({
  providers: [
    InternalBankTransferExecutor,
    InternalVaultTransferExecutor,
    {
      provide: STEP_EXECUTOR_PORT,
      useFactory: (bank: InternalBankTransferExecutor, vault: InternalVaultTransferExecutor) => [bank, vault],
      inject: [InternalBankTransferExecutor, InternalVaultTransferExecutor],
    },
    StepExecutorRegistry,
  ],
  exports: [StepExecutorRegistry],
})
export class SettlementModule {}
