import { Permission, ROLE_PERMISSIONS } from "@acme/types";

it('allows admins to retrigger webhooks', () => {
  expect(ROLE_PERMISSIONS.ADMIN).toContain(Permission.WEBHOOK_RETRIGGER);
});
