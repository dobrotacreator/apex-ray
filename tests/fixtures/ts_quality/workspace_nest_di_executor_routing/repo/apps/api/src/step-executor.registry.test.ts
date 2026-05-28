import { InternalBankTransferExecutor, InternalVaultTransferExecutor } from "@acme/settlement";
import { StepExecutorRegistry } from "./step-executor.registry";

it("routes each settlement step to the first executor that supports it", () => {
  const bank = new InternalBankTransferExecutor();
  const vault = new InternalVaultTransferExecutor();
  const registry = new StepExecutorRegistry([bank, vault]);

  expect(registry.getExecutor({ id: "step-1", type: "INTERNAL_BANK_TRANSFER" })?.name).toBe("bank");
  expect(registry.getExecutor({ id: "step-2", type: "INTERNAL_VAULT_TRANSFER" })?.name).toBe("vault");
});
