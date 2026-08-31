/**
 * Не даёт экрану уснуть, пока вкладка видима (Screen Wake Lock API, Firefox 126+).
 * Блокировка автоматически снимается браузером, когда вкладка уходит в фон,
 * поэтому перезапрашиваем её при каждом появлении вкладки на экране.
 */
let lock = null;

async function acquire() {
  if (!("wakeLock" in navigator) || lock) return;
  try {
    lock = await navigator.wakeLock.request("screen");
    lock.addEventListener("release", () => { lock = null; });
  } catch (e) {
    // Нет фокуса/разрешения — не критично, попробуем при следующем показе.
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") acquire();
});

acquire();
