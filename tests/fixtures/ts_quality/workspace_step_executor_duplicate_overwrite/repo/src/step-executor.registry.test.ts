import { describe, expect, it } from 'vitest';
import type { StepType as StepTypeT } from './step-type.js';
import { StepType } from './step-type.js';
import type { StepExecutorPort } from './step-executor.port.js';
import { StepExecutorRegistry } from './step-executor.registry.js';

describe('StepExecutorRegistry', () => {
  const makeExecutor = (stepType: StepTypeT, name: string): StepExecutorPort => ({
    stepType,
    dispatch: async () => name,
    reverse: async () => `${name}:reverse`,
  });

  it('registered step types are returned', () => {
    const exec = makeExecutor(StepType.INTERNAL_VAULT_TRANSFER, 'vault');
    const reg = new StepExecutorRegistry([exec]);
    expect(reg.resolve(StepType.INTERNAL_VAULT_TRANSFER)).toBe(exec);
    expect(reg.registeredStepTypes).toEqual(new Set([StepType.INTERNAL_VAULT_TRANSFER]));
  });

  it('rejects duplicate registration for same step type', () => {
    const first = makeExecutor(StepType.INTERNAL_VAULT_TRANSFER, 'first');
    const second = makeExecutor(StepType.INTERNAL_VAULT_TRANSFER, 'second');
    expect(() => new StepExecutorRegistry([first, second])).toThrow();
  });
});
