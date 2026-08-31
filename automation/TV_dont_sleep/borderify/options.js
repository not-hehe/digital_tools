const DEFAULTS = { displayMinutes: 30, reloadDelaySec: 5 };

async function load() {
  const s = { ...DEFAULTS, ...(await browser.storage.local.get(Object.keys(DEFAULTS))) };
  document.getElementById("displayMinutes").value = s.displayMinutes;
  document.getElementById("reloadDelaySec").value = s.reloadDelaySec;
}

document.getElementById("form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const displayMinutes = Math.max(1, parseInt(document.getElementById("displayMinutes").value, 10) || DEFAULTS.displayMinutes);
  const reloadDelaySec = Math.min(20, Math.max(0, parseInt(document.getElementById("reloadDelaySec").value, 10) ?? DEFAULTS.reloadDelaySec));
  await browser.storage.local.set({ displayMinutes, reloadDelaySec });
  const status = document.getElementById("status");
  status.textContent = "Сохранено";
  setTimeout(() => (status.textContent = ""), 2000);
});

document.addEventListener("DOMContentLoaded", load);
load();
