const ALARM = "carousel";

const DEFAULTS = {
  displayMinutes: 30, // минуты показа вкладки
  reloadDelaySec: 5,
  enabled: false,
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function getSettings() {
  const stored = await browser.storage.local.get(Object.keys(DEFAULTS));
  return { ...DEFAULTS, ...stored };
}

async function setBadge(enabled) {
  await browser.action.setBadgeText({ text: enabled ? "ON" : "OFF" });
  await browser.action.setBadgeBackgroundColor({
    color: enabled ? "#2e7d32" : "#757575",
  });
}

async function scheduleAlarm() {
  const s = await getSettings();
  await browser.alarms.clear(ALARM);
  browser.alarms.create(ALARM, {
    periodInMinutes: Math.max(1, s.displayMinutes),
  });
}

async function start() {
  await browser.storage.local.set({ enabled: true });
  await scheduleAlarm();
  await setBadge(true);
  await tick(); // первую вкладку показываем сразу, не ждём интервал
}

async function stop() {
  await browser.storage.local.set({ enabled: false });
  await browser.alarms.clear(ALARM);
  await setBadge(false);
}

/** Один шаг карусели: выбрать следующую живую вкладку, обновить, показать. */
async function tick() {
  const s = await getSettings();
  if (!s.enabled) return;

  const tabs = (await browser.tabs.query({ currentWindow: true })).sort(
    (a, b) => a.index - b.index
  );
  if (tabs.length === 0) return;

  const { lastTabId } = await browser.storage.local.get("lastTabId");
  const lastIdx = tabs.findIndex((t) => t.id === lastTabId); // -1, если вкладки уже нет

  // Пробуем вкладки по кругу: закрытые/сломанные пропускаем, а не умираем.
  for (let step = 1; step <= tabs.length; step++) {
    const tab = tabs[(lastIdx + step) % tabs.length];
    try {
      await browser.tabs.reload(tab.id);
      await sleep(Math.min(s.reloadDelaySec, 20) * 1000);
      await browser.tabs.update(tab.id, { active: true });
      await browser.storage.local.set({ lastTabId: tab.id });
      console.log(`TV Don't Sleep: показана вкладка «${tab.title}»`);
      return;
    } catch (e) {
      console.warn(`TV Don't Sleep: вкладка недоступна, пропускаю`, e);
    }
  }
}

// --- Слушатели -----------------------------------------------------------

browser.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) tick().catch(console.error);
});

// Клик по иконке — тумблер вкл/выкл.
browser.action.onClicked.addListener(async () => {
  const s = await getSettings();
  await (s.enabled ? stop() : start());
});

// Новый интервал из настроек применяем на лету.
browser.storage.onChanged.addListener(async (changes, area) => {
  if (area !== "local" || !changes.displayMinutes) return;
  const s = await getSettings();
  if (s.enabled) await scheduleAlarm();
});

// Восстановление состояния после перезапуска браузера или выгрузки фона.
async function restore() {
  const s = await getSettings();
  await setBadge(s.enabled);
  if (s.enabled && !(await browser.alarms.get(ALARM))) {
    await scheduleAlarm();
  }
}

browser.runtime.onStartup.addListener(() => restore().catch(console.error));
browser.runtime.onInstalled.addListener(() => restore().catch(console.error));
restore().catch(console.error);
