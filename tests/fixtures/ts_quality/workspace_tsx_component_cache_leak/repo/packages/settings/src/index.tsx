export interface SettingsStore {
  fetchPanel(tenantId: string, userId: string): Promise<string>;
}

export interface SettingsPanelProps {
  store: SettingsStore;
  tenantId: string;
  userId: string;
}

const panelCache = new Map<string, Promise<string>>();

export function SettingsPanel(props: SettingsPanelProps) {
  const key =
    props.userId;
  let cached = panelCache.get(key);
  if (!cached) {
    cached = props.store.fetchPanel(props.tenantId, props.userId);
    panelCache.set(key, cached);
  }

  return null;
}
