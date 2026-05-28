import { AdminWebhookOpsService } from "./index";

it("keeps retrigger keys actor scoped", () => {
  const service = new AdminWebhookOpsService();

  expect(service.retrigger("inbox-a", "operator-a")).toBe("operator-a:inbox-a");
});
