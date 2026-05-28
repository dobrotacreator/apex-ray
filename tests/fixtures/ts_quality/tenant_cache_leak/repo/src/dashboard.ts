import { loadUserSettings, SettingsStore } from "./settings";

export async function renderDashboard(
  store: SettingsStore,
  tenantId: string,
  userId: string,
): Promise<string> {
  const settings = await loadUserSettings(store, tenantId, userId);
  return `${tenantId}:${userId}:${settings.theme}`;
}
