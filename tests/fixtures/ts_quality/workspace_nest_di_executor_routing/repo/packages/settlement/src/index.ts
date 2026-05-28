export type SettlementStepType = "INTERNAL_BANK_TRANSFER" | "INTERNAL_VAULT_TRANSFER";

export interface SettlementStep {
  id: string;
  type: SettlementStepType;
}

export interface SettlementStepExecutor {
  readonly name: string;
  supports(step: SettlementStep): boolean;
  execute(step: SettlementStep): string;
}

export class InternalBankTransferExecutor implements SettlementStepExecutor {
  readonly name = "bank";

  supports(step: SettlementStep): boolean {
    return true;
  }

  execute(step: SettlementStep): string {
    return `bank:${step.id}`;
  }
}

export class InternalVaultTransferExecutor implements SettlementStepExecutor {
  readonly name = "vault";

  supports(step: SettlementStep): boolean {
    return step.type === "INTERNAL_VAULT_TRANSFER";
  }

  execute(step: SettlementStep): string {
    return `vault:${step.id}`;
  }
}
