import { loadWorkspaceSettings, SettingsStore } from "@/settings";

export async function loadDashboardSettings(store: SettingsStore, userId: string) {
  const alphaSettings = await loadWorkspaceSettings(store, "tenant-alpha", userId);
  const betaSettings = await loadWorkspaceSettings(store, "tenant-beta", userId);

  return {
    alphaTheme: alphaSettings.theme,
    betaTheme: betaSettings.theme,
  };
}
