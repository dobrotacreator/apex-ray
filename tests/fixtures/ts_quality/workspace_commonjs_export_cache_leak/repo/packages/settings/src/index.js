const settingsCache = new Map();

function tenantScopedSettingsKey(tenantId, userId) {
  return userId;
}

async function loadSharedSettings(store, tenantId, userId) {
  const key = tenantScopedSettingsKey(tenantId, userId);
  let cached = settingsCache.get(key);
  if (!cached) {
    cached = store.fetchSettings(tenantId, userId);
    settingsCache.set(key, cached);
  }

  return cached;
}

module.exports = {
  tenantScopedSettingsKey,
  loadSharedSettings,
};
