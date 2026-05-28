import { Permission } from "@acme/types";

it('still protects webhook retrigger', () => {
  expect(Permission.WEBHOOK_RETRIGGER).toBe('WEBHOOK_RETRIGGER');
});
