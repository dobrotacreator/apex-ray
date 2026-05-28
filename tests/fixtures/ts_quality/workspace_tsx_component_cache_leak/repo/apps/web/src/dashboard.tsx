import { SettingsPanel, type SettingsStore } from "@acme/settings";

export function Dashboard({ store, userId }: { store: SettingsStore; userId: string }) {
  return (
    <>
      <SettingsPanel store={store} tenantId="tenant-alpha" userId={userId} />
      <SettingsPanel store={store} tenantId="tenant-beta" userId={userId} />
    </>
  );
}
