const API_BASE = "";
const HORIZON_CANDLES = 12;
const TIMEFRAME_MS = 15 * 60 * 1000;

let accessToken = "";
let refreshToken = "";
let notifierSettings = null;

const byId = (id) => document.getElementById(id);
const serviceStatus = byId("service-status");

function setServiceStatus(state, label) {
  serviceStatus.classList.remove("online", "offline");
  if (state) serviceStatus.classList.add(state);
  serviceStatus.lastElementChild.textContent = label;
}

async function checkService() {
  try {
    const response = await fetch(`${API_BASE}/api/v1/ping`, { signal: AbortSignal.timeout(5000) });
    if (!response.ok) throw new Error("API недоступен");
    setServiceStatus("online", "Движок доступен");
  } catch {
    setServiceStatus("offline", "Движок не запущен");
  }
}

async function checkNotifier() {
  try {
    const response = await fetch(`${API_BASE}/notifier/health`, { signal: AbortSignal.timeout(5000), cache: "no-store" });
    if (!response.ok) throw new Error("Notifier недоступен");
    notifierSettings = await response.json();
    const status = byId("telegram-status");
    const configured = notifierSettings.telegram_configured;
    status.textContent = configured ? "Telegram подключён" : "Нужна настройка";
    status.className = `telegram-status ${configured ? "connected" : "disconnected"}`;
    byId("test-telegram").disabled = !configured;
    const eligible = notifierSettings.validation_eligible_pairs || [];
    const validationText = !notifierSettings.validation_available
      ? "бэктест ещё не выполнен; сигналы заблокированы"
      : eligible.length
        ? `проверку прошли: ${eligible.join(", ")}`
        : "ни одна пара не прошла фильтры; сигналы заблокированы";
    const detail = configured
      ? `${validationText} · риск ${notifierSettings.risk_per_trade_pct}%`
      : "Добавьте token и chat ID в .env, затем перезапустите приложение";
    byId("telegram-detail").textContent = detail;
  } catch {
    notifierSettings = null;
    byId("telegram-status").textContent = "Сервис не запущен";
    byId("telegram-status").className = "telegram-status disconnected";
    byId("telegram-detail").textContent = "Запустите приложение и проверьте Docker Compose";
  }
}

function showError(message) {
  byId("analysis-error").textContent = message;
  byId("error-panel").classList.remove("hidden");
}

function hideError() {
  byId("error-panel").classList.add("hidden");
  byId("analysis-error").textContent = "";
}

function setBusy(busy, title = "Готовим анализ", message = "Загружаем свечи и рассчитываем признаки модели.") {
  byId("loading-title").textContent = title;
  byId("loading-copy").textContent = message;
  byId("loading-panel").classList.toggle("hidden", !busy);
  byId("analyze-button").disabled = busy;
}

async function login(username, password) {
  const response = await fetch(`${API_BASE}/api/v1/token/login`, {
    method: "POST",
    headers: { Authorization: `Basic ${encodeBasicCredentials(username, password)}` },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || "Не удалось войти в локальный API.");
  accessToken = body.access_token;
  refreshToken = body.refresh_token;
  byId("login-panel").classList.add("hidden");
  byId("logout-button").classList.remove("hidden");
  setServiceStatus("online", "Подключено к движку");
}

async function refreshAccessToken() {
  if (!refreshToken) throw new Error("Сессия завершилась. Подключитесь снова.");
  const response = await fetch(`${API_BASE}/api/v1/token/refresh`, {
    method: "POST",
    headers: { Authorization: `Bearer ${refreshToken}` },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error("Сессия завершилась. Подключитесь снова.");
  accessToken = body.access_token;
}

function encodeBasicCredentials(username, password) {
  const bytes = new TextEncoder().encode(`${username}:${password}`);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

async function requestHistory(pair, mayRefresh = true) {
  const response = await fetch(`${API_BASE}/api/v1/pair_candles`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      pair,
      timeframe: "15m",
      limit: 1500,
      columns: [
      "date", "close", "volume", "ema_fast", "ema_slow", "rsi", "atr_pct",
        "forecast_return", "forecast_return_pct", "forecast_price", "do_predict",
      ],
    }),
  });
  const body = await response.json().catch(() => ({}));
  if (response.status === 401 && mayRefresh) {
    await refreshAccessToken();
    return requestHistory(pair, false);
  }
  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : "Проверьте конфигурацию FreqAI и доступность пары.";
    throw new Error(detail);
  }
  return body;
}

function fieldIndex(columns, name) {
  const index = columns.indexOf(name);
  if (index < 0) throw new Error(`В ответе модели отсутствует поле «${name}».`);
  return index;
}

function rowsFromResponse(result) {
  const columns = result.columns || [];
  const indexes = Object.fromEntries(
    ["date", "close", "volume", "ema_fast", "ema_slow", "rsi", "atr_pct", "forecast_return", "forecast_return_pct", "forecast_price", "do_predict"]
      .map((field) => [field, fieldIndex(columns, field)]),
  );
  return (result.data || []).map((row) => ({
    date: row[indexes.date],
    close: Number(row[indexes.close]),
    volume: Number(row[indexes.volume]),
    emaFast: Number(row[indexes.ema_fast]),
    emaSlow: Number(row[indexes.ema_slow]),
    rsi: Number(row[indexes.rsi]),
    atrPct: Number(row[indexes.atr_pct]),
    expectedReturn: Number(row[indexes.forecast_return]),
    expectedReturnPct: Number(row[indexes.forecast_return_pct]),
    expectedPrice: Number(row[indexes.forecast_price]),
    doPredict: Number(row[indexes.do_predict]),
  })).filter((row) => Number.isFinite(row.close) && row.close > 0);
}

function currency(value) {
  return new Intl.NumberFormat("ru-RU", { style: "currency", currency: "USD", maximumFractionDigits: value < 1 ? 6 : 2 }).format(value);
}

function drawChart(rows, prediction) {
  const container = byId("chart");
  const points = rows.slice(-96);
  const width = 1000;
  const height = 300;
  const pad = { top: 18, right: 14, bottom: 28, left: 66 };
  const values = points.map((point) => point.close).concat(prediction.expectedPrice);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const spread = Math.max(high - low, high * 0.001);
  const min = low - spread * 0.12;
  const max = high + spread * 0.12;
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;
  const x = (index) => pad.left + (index / Math.max(points.length, 1)) * plotWidth;
  const y = (price) => pad.top + ((max - price) / (max - min)) * plotHeight;
  const pricePath = points.map((point, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(point.close).toFixed(1)}`).join(" ");
  const lastX = x(points.length - 1);
  const forecastX = Math.min(width - pad.right, lastX + plotWidth * 0.16);
  const forecastY = y(prediction.expectedPrice);
  const grid = [0, 1, 2, 3].map((step) => {
    const yy = pad.top + (step / 3) * plotHeight;
    const price = max - (step / 3) * (max - min);
    return `<line class="chart-grid" x1="${pad.left}" y1="${yy}" x2="${width - pad.right}" y2="${yy}"/><text class="chart-label" x="0" y="${yy + 4}">${currency(price)}</text>`;
  }).join("");

  container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" aria-hidden="true">
    ${grid}
    <path class="price-line" d="${pricePath}"/>
    <path class="forecast-line" d="M${lastX.toFixed(1)},${y(points.at(-1).close).toFixed(1)} L${forecastX.toFixed(1)},${forecastY.toFixed(1)}"/>
    <circle class="forecast-point" cx="${forecastX.toFixed(1)}" cy="${forecastY.toFixed(1)}" r="5"/>
    <text class="chart-label" x="${pad.left}" y="${height - 4}">−24 ч</text>
    <text class="chart-label" x="${width - pad.right - 46}" y="${height - 4}">сейчас</text>
  </svg>`;
}

function renderResult(pair, result) {
  const rows = rowsFromResponse(result);
  if (!rows.length) throw new Error("Биржа не вернула свечи для этой пары.");
  const latest = rows.at(-1);
  const candleCloseMs = new Date(latest.date).getTime() + TIMEFRAME_MS;
  const candleAgeMs = Date.now() - candleCloseMs;
  const forecastValid = latest.doPredict === 1
    && Number.isFinite(latest.expectedReturn)
    && candleAgeMs >= 0
    && candleAgeMs <= TIMEFRAME_MS * 2 + 5 * 60 * 1000;
  const validationPassed = notifierSettings?.validation_eligible_pairs?.includes(pair) === true;
  const horizonHours = (HORIZON_CANDLES * TIMEFRAME_MS) / 3_600_000;
  const direction = !forecastValid || !validationPassed
    ? { text: validationPassed ? "Нет свежего сигнала" : "Проверка не пройдена", tone: "neutral" }
    : Math.abs(latest.expectedReturn) < 0.005
    ? { text: "Боковой сценарий", tone: "neutral" }
    : latest.expectedReturn > 0
      ? { text: "Возможен рост", tone: "up" }
      : { text: "Возможна коррекция", tone: "down" };

  byId("results").classList.remove("hidden");
  byId("result-pair").textContent = pair;
  byId("result-timestamp").textContent = `Свеча ${new Date(latest.date).toLocaleString("ru-RU")}`;
  byId("direction-value").textContent = forecastValid ? direction.text : "Нет сигнала";
  byId("direction-value").className = `direction-value ${forecastValid ? direction.tone : "neutral"}`;
  byId("direction-horizon").textContent = `Горизонт: ${horizonHours} ч · ${HORIZON_CANDLES} свечей`;
  byId("return-value").textContent = forecastValid ? `${latest.expectedReturnPct >= 0 ? "+" : ""}${latest.expectedReturnPct.toFixed(2)}%` : "—";
  byId("return-value").className = `metric-value ${forecastValid ? (latest.expectedReturnPct >= 0 ? "positive" : "negative") : ""}`;
  byId("price-value").textContent = forecastValid ? currency(latest.expectedPrice) : "—";
  byId("current-price").textContent = `Текущая цена: ${currency(latest.close)}`;
  byId("quality-value").textContent = forecastValid ? (validationPassed ? "Проверен" : "Не проверен") : "Фильтр модели";
  byId("quality-value").className = `quality-value ${forecastValid ? "" : "low"}`;
  byId("quality-detail").textContent = forecastValid
    ? (validationPassed ? "walk-forward тест прошёл фильтры" : "do_predict=1 · нет положительного бэктеста")
    : "Модель отклонила или устарела свеча";
  byId("rsi-value").textContent = Number.isFinite(latest.rsi) ? latest.rsi.toFixed(1) : "—";
  byId("trend-value").textContent = Number.isFinite(latest.emaFast) && Number.isFinite(latest.emaSlow)
    ? (latest.emaFast >= latest.emaSlow ? "EMA 20 выше EMA 50" : "EMA 20 ниже EMA 50")
    : "Недостаточно данных";
  byId("candle-count").textContent = new Intl.NumberFormat("ru-RU").format(rows.length);
  const canEstimate = forecastValid && validationPassed && latest.expectedReturn >= 0.005 && latest.atrPct > 0;
  if (canEstimate && notifierSettings) {
    const stopFraction = Math.min(0.03, Math.max(0.0075, latest.atrPct * 2));
    const equity = Number(notifierSettings.equity_usdt);
    const riskFraction = Number(notifierSettings.risk_per_trade_pct) / 100;
    const capFraction = Number(notifierSettings.max_position_pct) / 100;
    const maxPosition = Number(notifierSettings.max_position_usdt);
    const position = Math.min(equity * riskFraction / stopFraction, equity * capFraction, maxPosition);
    byId("position-size").textContent = `${position.toFixed(2)} USDT (paper)`;
    byId("stop-price").textContent = currency(latest.close * (1 - stopFraction));
    byId("risk-amount").textContent = `до ${(position * stopFraction).toFixed(2)} USDT`;
  } else {
    byId("position-size").textContent = "Нет расчёта";
    byId("stop-price").textContent = "—";
    byId("risk-amount").textContent = "—";
  }
  if (forecastValid) drawChart(rows, latest);
  else drawChart(rows, { expectedPrice: latest.close });
}

byId("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = byId("login-error");
  error.textContent = "";
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  try {
    await login(byId("username").value.trim(), byId("password").value);
  } catch (exception) {
    error.textContent = exception.message;
  } finally {
    button.disabled = false;
  }
});

byId("analysis-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  hideError();
  byId("results").classList.add("hidden");
  setBusy(true, "Загружаем расчёт модели", "Получаем свежие свечи и прогноз работающей dry-run модели.");
  try {
    if (!accessToken) throw new Error("Сначала подключитесь к локальному движку.");
    const pair = byId("pair").value;
    const result = await requestHistory(pair);
    renderResult(pair, result);
  } catch (exception) {
    showError(exception.message || "Неизвестная ошибка анализа.");
    if (exception.message.includes("Подключитесь снова")) {
      accessToken = "";
      refreshToken = "";
      byId("login-panel").classList.remove("hidden");
      byId("logout-button").classList.add("hidden");
    }
  } finally {
    setBusy(false);
  }
});

byId("logout-button").addEventListener("click", () => {
  accessToken = "";
  refreshToken = "";
  byId("login-panel").classList.remove("hidden");
  byId("logout-button").classList.add("hidden");
  byId("results").classList.add("hidden");
});

byId("test-telegram").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const message = byId("telegram-message");
  button.disabled = true;
  message.textContent = "Отправляем тестовое сообщение…";
  message.className = "telegram-message";
  try {
    const response = await fetch(`${API_BASE}/notifier/test`, { method: "POST", signal: AbortSignal.timeout(15000) });
    if (!response.ok) throw new Error("Не удалось отправить. Проверьте token, chat ID и доступ к Telegram.");
    message.textContent = "Тестовое сообщение отправлено. Проверьте чат с ботом.";
    message.classList.add("success");
  } catch (error) {
    message.textContent = error.message || "Сервис Telegram недоступен.";
    message.classList.add("failure");
  } finally {
    button.disabled = !notifierSettings?.telegram_configured;
  }
});

checkService();
checkNotifier();
window.setInterval(checkService, 15_000);
window.setInterval(checkNotifier, 30_000);
