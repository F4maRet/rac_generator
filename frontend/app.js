const API = "/api";
// Должна совпадать с APP_VERSION в backend/main.py (см. комментарий там).
const APP_VERSION = "2026.10.05-about";
const SECTION_TITLES = {
  section1: "1. Назначение предложения",
  section2: "2. Сведения о недостатках существующих технических решений (аналогов)",
  section3: "3. Описание предлагаемого технического решения",
  section4: "4. Графическое изображение предложения",
  section5: "5. Степень разработки предложения, масштаб и место его применения",
  section6: "6. Обоснование положительного эффекта от применения предложения",
};
const AUTHOR_FIELDS = ["rank", "full_name", "position", "workplace"];
const PROVIDER_TYPE_SHORT = { openai_compat: "OpenAI-совместимый", gemini: "Gemini", ollama: "Ollama" };

// Разбор отметки о лимите по данным последней проверки/использования модели
// (last_check_status/last_check_message уже обновляются и вручную кнопкой
// «проверить», и автоматически при реальной генерации - см. backend).
function limitStatus(m) {
  if (!m.last_check_status) return { key: "unknown", label: "неизвестно", cls: "limit-unknown" };
  if (m.last_check_status === "ok") return { key: "ok", label: "в норме", cls: "limit-ok" };
  const msg = (m.last_check_message || "").toLowerCase();
  if (msg.includes("лимит")) return { key: "exceeded", label: "исчерпан", cls: "limit-exceeded" };
  return { key: "other_error", label: "—", cls: "limit-unknown" };
}

let state = {
  generated: null,
  lastUsedModelName: "",
  progressHideTimer: null,
  activeGeneration: null, // { requestId, controller } во время генерации
  pickerAuthors: [],       // полный кэш авторов для автодополнения/модалки (шаг 4)
  pickerSignatories: [],   // полный кэш подписантов для автодополнения/модалки (шаг 6)
  slotSelections: [null, null, null, null, null, null], // выбранный автор на каждую из 6 позиций
  activeSlotIndex: null,   // какую позицию сейчас заполняет модальное окно
  authorsTableCache: [],   // кэш для вкладки "Авторы" (клиентская фильтрация)
  modelsCache: [],         // кэш для вкладки "Настройки" (клиентская фильтрация)
  savedEndpoints: [],
  llmProviderTypeLabels: {},
  modelSort: { field: "priority", dir: "asc" }, // сортировка таблицы моделей: приоритет по умолчанию
  modelLogModelId: null,
  modelLogOffset: 0,
};

// ---------------- tabs ----------------
document.getElementById("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  btn.classList.add("active");
  document.getElementById("view-" + btn.dataset.tab).classList.add("active");
  if (btn.dataset.tab === "new") {
    refreshAuthorsCache();
    refreshSignatoriesCache();
    initProviderSelect(); // отметки лимита могли обновиться на вкладке "Настройки" - подтягиваем свежие
  }
  if (btn.dataset.tab === "authors") loadAuthorsTable();
  if (btn.dataset.tab === "history") loadHistory();
  if (btn.dataset.tab === "settings") loadSettings();
});

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = formatErrorDetail(j.detail) || msg;
    } catch (_) {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

// detail в ответе FastAPI - обычно строка, но у ошибок проверки ввода может
// быть списком объектов: раньше интерфейс показывал его как «[object Object]»
function formatErrorDetail(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => (d && d.msg) || String(d)).join("; ");
  return detail.message || JSON.stringify(detail);
}

// ==================== SECTIONS UI ====================
function renderSections(sections) {
  const wrap = document.getElementById("sectionsWrap");
  wrap.innerHTML = "";
  Object.entries(SECTION_TITLES).forEach(([key, title]) => {
    const block = document.createElement("div");
    block.className = "section-block";
    block.innerHTML = `<label>${title}</label><textarea data-key="${key}">${sections?.[key] || ""}</textarea>`;
    wrap.appendChild(block);
  });
}
renderSections({});

function getSectionsFromForm() {
  const out = {};
  document.querySelectorAll("#sectionsWrap textarea").forEach((ta) => {
    out[ta.dataset.key] = ta.value;
  });
  return out;
}

// ==================== MODELS FOR GENERATION DROPDOWN ====================
async function loadModelOptions(selectEl) {
  const previousValue = selectEl.value; // сохраняем выбор пользователя - см. ниже, почему это важно
  const models = await api("/models");
  const enabled = models.filter((m) => m.enabled);
  const limitMark = { ok: "✓ лимит ок", exceeded: "⚠ лимит исчерпан", unknown: "лимит неизв.", other_error: "лимит неизв." };
  const options = [`<option value="">Автоматически (по приоритету)</option>`]
    .concat(
      enabled.map((m) => {
        const ls = limitStatus(m);
        return `<option value="${m.id}">${escapeHtml(m.name)}${m.is_paid ? " · платная" : " · бесплатная"} · ${limitMark[ls.key]}</option>`;
      })
    );
  selectEl.innerHTML = options.join("");
  // без этого условия перерисовка списка (например, после пинга или правки
  // другой модели) молча сбрасывала бы выбор пользователя обратно на
  // "Автоматически" - реальный баг, а не просто мелочь
  if (previousValue && [...selectEl.options].some((o) => o.value === previousValue)) {
    selectEl.value = previousValue;
  }
}

async function initProviderSelect() {
  await loadModelOptions(document.getElementById("providerSelect"));
}
initProviderSelect();

// ==================== GENERATE TEXT (потоково, с прогресс-баром) ====================

const PROGRESS_STAGES = ["sending", "received", "forming", "done"];
const PROGRESS_LABELS = {
  sending: (model) => `Отправляем запрос модели «${model}»...`,
  escalating: (model, maxTokens) => `Ответ «${model}» обрезан лимитом токенов — пробуем снова с увеличенным лимитом (~${maxTokens})...`,
  rate_limited: (model, seconds) => `«${model}» — лимит частоты запросов, ждём ${seconds} сек. и пробуем тот же запрос снова...`,
  received: (model) => `Ответ от «${model}» получен, разбираем...`,
  forming: () => "Формируем текст разделов...",
  done: (model) => `Готово. Текст сгенерирован моделью «${model}».`,
  retry: (model, error) => `«${model}» — ${error}. Переключаемся на следующую нейросеть...`,
  error: (message) => message,
};

// ---------- восстановление результата после перезагрузки страницы ----------
// Генерация теперь выполняется в фоновой задаче на сервере, не привязанной к
// жизни HTTP-соединения (см. backend/main.py, _run_generation_task) - поэтому
// закрытие вкладки или обновление страницы посреди долгого перебора моделей
// больше не обязательно теряет уже потраченный лимит нейросети без всякого
// результата: при следующей загрузке страницы можно забрать итог по
// сохранённому request_id через GET /api/generate/result/{id}.
const PENDING_GENERATION_KEY = "rac_pending_generation";

function savePendingGeneration(requestId, topic) {
  try {
    localStorage.setItem(PENDING_GENERATION_KEY, JSON.stringify({ requestId, topic, savedAt: Date.now() }));
  } catch (_) {}
}

function clearPendingGeneration() {
  try {
    localStorage.removeItem(PENDING_GENERATION_KEY);
  } catch (_) {}
}

async function checkRecoverableGeneration() {
  let pending;
  try {
    const raw = localStorage.getItem(PENDING_GENERATION_KEY);
    if (!raw) return;
    pending = JSON.parse(raw);
  } catch (_) {
    return;
  }
  if (!pending?.requestId) {
    clearPendingGeneration();
    return;
  }
  const status = document.getElementById("generateStatus");
  try {
    const result = await api(`/generate/result/${pending.requestId}`);
    if (result.status === "done" && result.data) {
      document.getElementById("titleInput").value = result.data.title;
      state.generated = result.data;
      state.lastUsedModelName = result.data._used_model?.name || "";
      await revealSectionsStaggered(result.data);
      status.textContent =
        `Восстановлен результат генерации (тема: «${pending.topic}»), прерванной до перезагрузки страницы. ` +
        `Текст сгенерирован моделью «${state.lastUsedModelName}».`;
      status.className = "hint ok";
      clearPendingGeneration();
    } else if (result.status === "error" || result.status === "cancelled") {
      status.textContent = `Ранее начатая генерация (тема: «${pending.topic}») завершилась без результата: ${result.message || result.status}.`;
      status.className = "hint error";
      clearPendingGeneration();
    } else {
      // ещё выполняется на сервере - проверим ещё раз чуть позже; если и тогда
      // не готово, пользователь просто обновит страницу вручную
      status.textContent = `Генерация (тема: «${pending.topic}»), начатая до перезагрузки страницы, ещё выполняется на сервере...`;
      status.className = "hint";
      setTimeout(checkRecoverableGeneration, 15000);
    }
  } catch (e) {
    // 404 - такой генерации нет (либо очень старая и уже подчищена) - дальше смотреть нечего
    clearPendingGeneration();
  }
}
checkRecoverableGeneration();

function setProgressStage(stage, { failed = false } = {}) {
  const idx = PROGRESS_STAGES.indexOf(stage);
  document.querySelectorAll("#genProgressBar .progress-segment").forEach((seg, i) => {
    seg.classList.remove("active", "current", "failed");
    if (failed) {
      if (i <= idx) seg.classList.add("active");
      if (i === idx) seg.classList.add("failed");
      return;
    }
    if (i < idx) seg.classList.add("active");
    else if (i === idx) {
      seg.classList.add("active");
      if (stage !== "done") seg.classList.add("current");
    }
  });
}

function resetProgressBar() {
  document.querySelectorAll("#genProgressBar .progress-segment").forEach((seg) => {
    seg.classList.remove("active", "current", "failed");
  });
  document.getElementById("genProgress").classList.remove("hidden");
  document.getElementById("genProgressLabel").textContent = "";
}

async function revealSectionsStaggered(data) {
  const keys = Object.keys(SECTION_TITLES);
  renderSections({});
  const label = document.getElementById("genProgressLabel");
  for (let i = 0; i < keys.length; i++) {
    label.textContent = `Формируем раздел ${i + 1} из ${keys.length}: «${SECTION_TITLES[keys[i]].replace(/^\d+\.\s*/, "")}»...`;
    await new Promise((r) => setTimeout(r, 110));
    const ta = document.querySelector(`#sectionsWrap textarea[data-key="${keys[i]}"]`);
    if (ta) ta.value = data[keys[i]] || "";
  }
}

document.getElementById("btnGenerate").addEventListener("click", async () => {
  // кнопка работает как переключатель: если генерация уже идёт - это клик
  // "Остановить генерацию", а не повторный запуск
  if (state.activeGeneration) {
    stopGeneration("Останавливаем...");
    return;
  }

  const topic = document.getElementById("topic").value.trim();
  const extra = document.getElementById("extraContext").value.trim();
  const preferredId = document.getElementById("providerSelect").value || null;
  const status = document.getElementById("generateStatus");
  const label = document.getElementById("genProgressLabel");
  status.textContent = "";
  status.className = "hint";
  if (!topic) {
    status.textContent = "Опишите тему предложения.";
    status.className = "hint error";
    return;
  }

  const requestId = genRequestId();
  const controller = new AbortController();
  state.activeGeneration = { requestId, controller };
  savePendingGeneration(requestId, topic);

  setGenerateButtonMode("stop");
  document.getElementById("topic").disabled = true;
  document.getElementById("extraContext").disabled = true;
  document.getElementById("providerSelect").disabled = true;
  if (state.progressHideTimer) clearTimeout(state.progressHideTimer);
  resetProgressBar();
  setProgressStage("sending");

  try {
    const res = await fetch(API + "/generate/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        topic, extra_context: extra,
        preferred_model_id: preferredId ? Number(preferredId) : null,
        request_id: requestId,
      }),
      signal: controller.signal,
    });
    if (!res.ok || !res.body) {
      let msg = res.statusText;
      try {
        msg = (await res.json()).detail || msg;
      } catch (_) {}
      throw new Error(msg);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finalData = null;
    let finalError = null;
    let wasCancelled = false;

    const handleEvent = async (ev) => {
      if (ev.stage === "sending") {
        setProgressStage("sending");
        label.textContent = PROGRESS_LABELS.sending(ev.model);
      } else if (ev.stage === "escalating") {
        // остаёмся визуально на той же стадии ("отправка") - это повтор той
        // же модели с бОльшим лимитом токенов, а не переключение на другую
        setProgressStage("sending");
        label.textContent = PROGRESS_LABELS.escalating(ev.model, ev.max_tokens);
      } else if (ev.stage === "rate_limited") {
        // короткое ожидание по Retry-After перед повтором того же запроса -
        // визуально та же стадия "отправка", просто с другой подписью
        setProgressStage("sending");
        label.textContent = PROGRESS_LABELS.rate_limited(ev.model, ev.seconds);
      } else if (ev.stage === "received") {
        setProgressStage("received");
        label.textContent = PROGRESS_LABELS.received(ev.model);
      } else if (ev.stage === "forming") {
        setProgressStage("forming");
        label.textContent = PROGRESS_LABELS.forming();
      } else if (ev.stage === "retry") {
        setProgressStage("sending", { failed: true });
        label.textContent = PROGRESS_LABELS.retry(ev.model, ev.error);
        await new Promise((r) => setTimeout(r, 500)); // дать пользователю прочитать, что произошло переключение
        resetProgressBar();
      } else if (ev.stage === "done") {
        finalData = ev.data;
        await revealSectionsStaggered(finalData);
        setProgressStage("done");
        label.textContent = PROGRESS_LABELS.done(finalData._used_model?.name || "");
      } else if (ev.stage === "cancelled") {
        wasCancelled = true;
        label.textContent = ev.message;
      } else if (ev.stage === "error") {
        finalError = ev.message;
        setProgressStage(PROGRESS_STAGES[PROGRESS_STAGES.length - 1], { failed: true });
        label.textContent = ev.message;
      }
    };

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, idx).trim();
        buffer = buffer.slice(idx + 1);
        if (line) await handleEvent(JSON.parse(line));
      }
    }
    if (buffer.trim()) await handleEvent(JSON.parse(buffer.trim()));

    if (wasCancelled) {
      status.textContent = "Генерация остановлена — лимит дальнейших моделей в цепочке не расходовался.";
      status.className = "hint";
    } else {
      if (finalError) throw new Error(finalError);
      if (!finalData) throw new Error("Генерация не вернула результат.");

      document.getElementById("titleInput").value = finalData.title;
      state.generated = finalData;
      state.lastUsedModelName = finalData._used_model?.name || "";

      const failedBefore = (finalData._attempts || []).filter((a) => !a.ok);
      let msg = `Готово. Текст сгенерирован моделью «${state.lastUsedModelName}».`;
      if (finalData._max_tokens_escalated_to) {
        msg += ` Ответ обрезался при обычном лимите — лимит токенов у этой модели автоматически увеличен до ${finalData._max_tokens_escalated_to} и запомнен на будущее.`;
      }
      if (failedBefore.length) {
        msg += " Переключение произошло автоматически: " + failedBefore.map((a) => `«${a.name}» — ${a.error}`).join("; ") + ".";
      }
      status.textContent = msg;
      status.className = "hint " + (failedBefore.length ? "error" : "ok");
    }
  } catch (e) {
    if (e.name === "AbortError") {
      // пользователь сам остановил - это не ошибка, отдельно уведомляем бэкенд
      // (см. stopGeneration), тут просто не показываем "Ошибка: ..."
      status.textContent = "Генерация остановлена — лимит дальнейших моделей в цепочке не расходовался.";
      status.className = "hint";
    } else {
      status.textContent = "Ошибка: " + e.message;
      status.className = "hint error";
    }
  } finally {
    state.activeGeneration = null;
    setGenerateButtonMode("generate");
    document.getElementById("topic").disabled = false;
    document.getElementById("extraContext").disabled = false;
    document.getElementById("providerSelect").disabled = false;
    state.progressHideTimer = setTimeout(() => document.getElementById("genProgress").classList.add("hidden"), 2500);
    // Соединение дожило до конца (успех/ошибка/отмена/обрыв клиента) в рамках
    // этого же вызова - отдельно восстанавливать результат по request_id при
    // следующей загрузке страницы уже не нужно. Если же сама вкладка закрылась
    // до этого момента, этот код просто не выполнится, и запись в localStorage
    // останется - для того и рассчитан checkRecoverableGeneration().
    clearPendingGeneration();
  }
});

function setGenerateButtonMode(mode) {
  const btn = document.getElementById("btnGenerate");
  if (mode === "stop") {
    btn.textContent = "⏹ Остановить генерацию";
    btn.classList.add("stop");
    btn.disabled = false;
  } else {
    btn.textContent = "Сгенерировать текст";
    btn.classList.remove("stop");
    btn.disabled = false;
  }
}

function genRequestId() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return "r" + Date.now().toString(36) + Math.random().toString(36).slice(2);
}

function stopGeneration(interimLabel) {
  if (!state.activeGeneration) return;
  const { requestId, controller } = state.activeGeneration;
  const btn = document.getElementById("btnGenerate");
  btn.disabled = true; // защита от повторного клика, пока идёт остановка
  if (interimLabel) document.getElementById("genProgressLabel").textContent = interimLabel;
  // сообщаем бэкенду ПЕРВЫМ, до обрыва соединения - иначе он может успеть
  // начать следующую попытку раньше, чем узнает об отмене
  api("/generate/cancel", { method: "POST", body: JSON.stringify({ request_id: requestId }) })
    .catch(() => {})
    .finally(() => controller.abort());
}

// ==================== AUTHOR PICKER (шаг 4: поиск-автодополнение + модалка-таблица) ====================

async function refreshAuthorsCache() {
  // Обновляет только кэш (для поиска и модалки), НЕ трогая уже сделанный пользователем
  // выбор/проценты в слотах - иначе при каждом переключении вкладки выбор бы слетал.
  try {
    state.pickerAuthors = await api("/authors");
  } catch (e) {
    state.pickerAuthors = [];
  }
}

function authorMatchesQuery(a, query) {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return AUTHOR_FIELDS.some((f) => (a[f] || "").toLowerCase().includes(q));
}

function buildAuthorsPicker() {
  const wrap = document.getElementById("authorsPicker");
  wrap.innerHTML = "";
  const defaultPercents = [10, 10, 10, 10, 40, 20];
  for (let i = 0; i < 6; i++) {
    const slot = document.createElement("div");
    slot.className = "author-slot";
    slot.dataset.idx = i;
    slot.innerHTML = `
      <div class="slot-label">Автор ${i + 1}</div>
      <div class="author-slot-input-row">
        <input type="text" class="author-search-input" placeholder="Начните вводить фамилию..." data-idx="${i}" autocomplete="off">
        <button type="button" class="btn btn-icon" data-action="open-picker" data-idx="${i}" title="Выбрать из таблицы">☰</button>
      </div>
      <div class="suggest-list hidden" data-idx="${i}"></div>
      <div class="percent-row">
        <input type="number" class="percent-input" min="0" max="100" step="1" value="${defaultPercents[i]}" data-idx="${i}">
        <span>%</span>
      </div>
    `;
    wrap.appendChild(slot);
  }
  wrap.querySelectorAll(".percent-input").forEach((inp) => inp.addEventListener("input", updatePercentStatus));
  wrap.querySelectorAll(".author-search-input").forEach((inp) => {
    inp.addEventListener("input", () => onAuthorSearchInput(Number(inp.dataset.idx)));
    inp.addEventListener("focus", () => onAuthorSearchInput(Number(inp.dataset.idx)));
  });
  wrap.querySelectorAll('[data-action="open-picker"]').forEach((btn) => {
    btn.addEventListener("click", () => openAuthorPickerModal(Number(btn.dataset.idx)));
  });
  updatePercentStatus();
}

function onAuthorSearchInput(idx) {
  const slot = document.querySelector(`.author-slot[data-idx="${idx}"]`);
  const input = slot.querySelector(".author-search-input");
  const list = slot.querySelector(".suggest-list");
  const query = input.value;

  // если текст разошёлся с ранее выбранным автором - выбор аннулируется,
  // чтобы нельзя было "сгенерировать документ" с несуществующим автором
  const sel = state.slotSelections[idx];
  if (sel && input.value.trim() !== sel.full_name) {
    state.slotSelections[idx] = null;
    slot.classList.remove("has-selection");
  }

  const matches = state.pickerAuthors.filter((a) => authorMatchesQuery(a, query)).slice(0, 8);
  if (!matches.length) {
    list.innerHTML = `<div class="suggest-empty">${state.pickerAuthors.length ? "Совпадений не найдено" : "Список авторов пуст — добавьте на вкладке «Авторы»"}</div>`;
  } else {
    list.innerHTML = matches
      .map(
        (a) => `<div class="suggest-item" data-id="${a.id}">
          <div class="s-name">${escapeHtml(a.full_name)}</div>
          <div class="s-meta">${escapeHtml([a.rank, a.position, a.workplace].filter(Boolean).join(" · "))}</div>
        </div>`
      )
      .join("");
  }
  list.classList.remove("hidden");
}

document.getElementById("authorsPicker").addEventListener("mousedown", (e) => {
  // mousedown+preventDefault, а не click - иначе клик по подсказке снимает
  // фокус с поля, браузер тут же возвращает его назад, и это заново
  // открывает список подсказок сразу после выбора (тот же баг, что нашёлся
  // у пикера подписанта - здесь чиним превентивно)
  const item = e.target.closest(".suggest-item");
  if (!item) return;
  e.preventDefault();
  const list = item.closest(".suggest-list");
  const idx = Number(list.dataset.idx);
  const author = state.pickerAuthors.find((a) => a.id === Number(item.dataset.id));
  if (author) selectAuthorForSlot(idx, author);
});

// клик вне поля поиска/списка - закрыть подсказки (только для слотов авторов;
// подсказки адреса base_url закрываются своим отдельным обработчиком ниже)
document.addEventListener("click", (e) => {
  if (e.target.closest(".author-slot")) return;
  document.querySelectorAll(".author-slot .suggest-list").forEach((l) => l.classList.add("hidden"));
});

function selectAuthorForSlot(idx, author) {
  state.slotSelections[idx] = author;
  const slot = document.querySelector(`.author-slot[data-idx="${idx}"]`);
  slot.querySelector(".author-search-input").value = author.full_name;
  slot.classList.add("has-selection");
  slot.querySelector(".suggest-list").classList.add("hidden");
}

function updatePercentStatus() {
  const status = document.getElementById("percentStatus");
  const percents = [...document.querySelectorAll(".percent-input")].map((i) => Number(i.value) || 0);
  const total = percents.reduce((a, b) => a + b, 0);
  status.textContent = `Сумма процентов: ${total}%` + (total !== 100 ? " (должна быть ровно 100%)" : " ✓");
  status.className = "hint " + (total === 100 ? "ok" : "error");
}

// ---------- модалка "выбрать автора из таблицы" ----------

function openAuthorPickerModal(idx) {
  state.activeSlotIndex = idx;
  document.getElementById("authorPickerSlotLabel").textContent = `— позиция «Автор ${idx + 1}»`;
  document.getElementById("authorPickerSearch").value = "";
  document.querySelectorAll("#authorPickerTable [data-filter]").forEach((el) => (el.value = ""));
  renderAuthorPickerTable();
  document.getElementById("authorPickerModal").classList.remove("hidden");
}

function closeAuthorPickerModal() {
  document.getElementById("authorPickerModal").classList.add("hidden");
  state.activeSlotIndex = null;
}
document.getElementById("authorPickerClose").addEventListener("click", closeAuthorPickerModal);
document.getElementById("authorPickerModal").addEventListener("click", (e) => {
  if (e.target.id === "authorPickerModal") closeAuthorPickerModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !document.getElementById("authorPickerModal").classList.contains("hidden")) {
    closeAuthorPickerModal();
  }
});

function renderAuthorPickerTable() {
  const globalQ = document.getElementById("authorPickerSearch").value;
  const colFilters = {};
  document.querySelectorAll("#authorPickerTable [data-filter]").forEach((el) => {
    colFilters[el.dataset.filter] = el.value.trim().toLowerCase();
  });
  const rows = state.pickerAuthors.filter((a) => {
    if (!authorMatchesQuery(a, globalQ)) return false;
    return AUTHOR_FIELDS.every((f) => !colFilters[f] || (a[f] || "").toLowerCase().includes(colFilters[f]));
  });
  const tbody = document.querySelector("#authorPickerTable tbody");
  tbody.innerHTML =
    rows
      .map(
        (a) => `<tr data-id="${a.id}">
          <td data-label="Звание">${escapeHtml(a.rank)}</td>
          <td data-label="ФИО">${escapeHtml(a.full_name)}</td>
          <td data-label="Должность">${escapeHtml(a.position)}</td>
          <td data-label="Место службы">${escapeHtml(a.workplace)}</td>
        </tr>`
      )
      .join("") || `<tr><td colspan="4">Ничего не найдено.</td></tr>`;
}

document.getElementById("authorPickerSearch").addEventListener("input", renderAuthorPickerTable);
document.querySelectorAll("#authorPickerTable [data-filter]").forEach((el) => el.addEventListener("input", renderAuthorPickerTable));

document.querySelector("#authorPickerTable tbody").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-id]");
  if (!row || state.activeSlotIndex === null) return;
  const author = state.pickerAuthors.find((a) => a.id === Number(row.dataset.id));
  if (author) {
    selectAuthorForSlot(state.activeSlotIndex, author);
    closeAuthorPickerModal();
  }
});

// первичная инициализация (слоты создаются один раз; кэш обновляется при каждом заходе на вкладку)
buildAuthorsPicker();
refreshAuthorsCache();

// ==================== SIGNATORY PICKER (блок 6: автокомплит + модалка, по образцу автора) ====================
// В отличие от авторов, тут НЕ требуется обязательный выбор из списка - ФИО
// подписанта просто текст, подставляемый в документ напрямую, без связанных
// с ним структурированных данных. Подсказки/модалка - только для удобства.

async function refreshSignatoriesCache() {
  try {
    state.pickerSignatories = await api("/signatories");
  } catch (e) {
    state.pickerSignatories = [];
  }
}

function renderSignatorySuggestions() {
  const input = document.getElementById("viceFio");
  const box = document.getElementById("signatorySuggest");
  const q = input.value.trim().toLowerCase();
  const matches = state.pickerSignatories.filter((s) => !q || s.full_name.toLowerCase().includes(q));
  if (!matches.length) {
    box.innerHTML = `<div class="suggest-empty">${state.pickerSignatories.length ? "Совпадений не найдено" : "Пока нет сохранённых подписантов — добавьте на вкладке «Настройки», или впишите ФИО прямо здесь"}</div>`;
  } else {
    box.innerHTML = matches
      .map((s) => `<div class="suggest-item" data-id="${s.id}"><div class="s-name">${escapeHtml(s.full_name)}</div></div>`)
      .join("");
  }
  box.classList.remove("hidden");
}

document.getElementById("viceFio").addEventListener("input", renderSignatorySuggestions);
document.getElementById("viceFio").addEventListener("focus", renderSignatorySuggestions);
document.getElementById("signatorySuggest").addEventListener("mousedown", (e) => {
  // mousedown (не click) + preventDefault - чтобы поле НЕ теряло фокус при
  // клике по подсказке: иначе браузер снимает фокус, а затем сам же
  // возвращает его обратно на #viceFio, что триггерит обработчик "focus" и
  // заново открывает список подсказок сразу после того, как мы его закрыли
  e.preventDefault();
  const item = e.target.closest(".suggest-item");
  if (!item) return;
  const s = state.pickerSignatories.find((x) => x.id === Number(item.dataset.id));
  if (s) document.getElementById("viceFio").value = s.full_name;
  document.getElementById("signatorySuggest").classList.add("hidden");
});
document.addEventListener("click", (e) => {
  if (e.target.closest("#viceFio") || e.target.closest("#signatorySuggest")) return;
  document.getElementById("signatorySuggest")?.classList.add("hidden");
});

// ---------- модалка "выбрать подписанта из списка" ----------

function openSignatoryPickerModal() {
  document.getElementById("signatoryPickerSearch").value = "";
  renderSignatoryPickerList();
  document.getElementById("signatoryPickerModal").classList.remove("hidden");
}

function closeSignatoryPickerModal() {
  document.getElementById("signatoryPickerModal").classList.add("hidden");
}

document.getElementById("btnOpenSignatoryPicker").addEventListener("click", openSignatoryPickerModal);
document.getElementById("signatoryPickerClose").addEventListener("click", closeSignatoryPickerModal);
document.getElementById("signatoryPickerModal").addEventListener("click", (e) => {
  if (e.target.id === "signatoryPickerModal") closeSignatoryPickerModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !document.getElementById("signatoryPickerModal").classList.contains("hidden")) {
    closeSignatoryPickerModal();
  }
});

function renderSignatoryPickerList() {
  const q = document.getElementById("signatoryPickerSearch").value.trim().toLowerCase();
  const list = document.getElementById("signatoryPickerList");
  const rows = state.pickerSignatories.filter((s) => !q || s.full_name.toLowerCase().includes(q));
  list.innerHTML =
    rows
      .map(
        (s) => `<div class="picker-list-item" data-id="${s.id}">
          <span>${escapeHtml(s.full_name)}</span>
          <button type="button" class="picker-item-delete" data-action="delete-signatory-inline" data-id="${s.id}" title="Удалить из сохранённых">✕</button>
        </div>`
      )
      .join("") ||
    `<div class="picker-list-empty">${state.pickerSignatories.length ? "Ничего не найдено." : "Список пуст — впишите ФИО в поле и сохраните кнопкой 💾, либо добавьте на вкладке «Настройки»."}</div>`;
}

document.getElementById("signatoryPickerSearch").addEventListener("input", renderSignatoryPickerList);

document.getElementById("signatoryPickerList").addEventListener("click", async (e) => {
  const delBtn = e.target.closest('[data-action="delete-signatory-inline"]');
  if (delBtn) {
    e.stopPropagation();
    await api("/signatories/" + delBtn.dataset.id, { method: "DELETE" });
    await refreshSignatoriesCache();
    renderSignatoryPickerList();
    return;
  }
  const item = e.target.closest(".picker-list-item");
  if (!item) return;
  const s = state.pickerSignatories.find((x) => x.id === Number(item.dataset.id));
  if (s) {
    document.getElementById("viceFio").value = s.full_name;
    closeSignatoryPickerModal();
  }
});

refreshSignatoriesCache();

// ==================== BUILD DOCUMENT ====================
document.getElementById("btnBuild").addEventListener("click", async () => {
  const status = document.getElementById("buildStatus");
  const title = document.getElementById("titleInput").value.trim();
  const viceHead = document.getElementById("viceHead").value.trim();
  const viceFio = document.getElementById("viceFio").value.trim();
  const sections = getSectionsFromForm();
  const topic = document.getElementById("topic").value.trim();

  if (!title || !(title.startsWith("Способ") || title.startsWith("Модель"))) {
    status.textContent = "Наименование должно начинаться со слова «Способ» или «Модель».";
    status.className = "hint error";
    return;
  }
  const missingSection = Object.keys(SECTION_TITLES).find((k) => !sections[k]?.trim());
  if (missingSection) {
    status.textContent = "Заполните все 6 разделов описания.";
    status.className = "hint error";
    return;
  }

  const percentInputs = [...document.querySelectorAll(".percent-input")];
  const authors = [];
  for (let i = 0; i < 6; i++) {
    const sel = state.slotSelections[i];
    if (!sel) {
      status.textContent = `Автор ${i + 1}: выберите автора из списка подсказок или через кнопку «☰» (просто ввод ФИО не считается выбором).`;
      status.className = "hint error";
      document.querySelector(`.author-slot[data-idx="${i}"]`)?.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    const raw = percentInputs[i].value.trim();
    const percent = Number(raw);
    // целое 0..100: раньше проходили дробные (сервер отвечал невнятной ошибкой)
    // и отрицательные (в документ попадало «-50 процентов»)
    if (raw === "" || !Number.isInteger(percent) || percent < 0 || percent > 100) {
      status.textContent = `Автор ${i + 1}: процент должен быть целым числом от 0 до 100.`;
      status.className = "hint error";
      percentInputs[i].focus();
      return;
    }
    authors.push({ author_id: sel.id, percent });
  }
  const authorIds = authors.map((a) => a.author_id);
  if (new Set(authorIds).size !== authorIds.length) {
    status.textContent = "Один и тот же автор выбран для нескольких позиций — выберите 6 разных человек.";
    status.className = "hint error";
    return;
  }
  const totalPercent = authors.reduce((a, b) => a + b.percent, 0);
  if (totalPercent !== 100) {
    status.textContent = `Сумма процентов должна быть равна 100 (сейчас ${totalPercent}).`;
    status.className = "hint error";
    return;
  }

  status.textContent = "Собираем документ...";
  status.className = "hint";
  document.getElementById("btnBuild").disabled = true;
  try {
    const result = await api("/proposals", {
      method: "POST",
      body: JSON.stringify({
        title,
        sections,
        authors,
        vice_head: viceHead || "Врио начальника",
        vice_fio: viceFio || "И.Иванов",
        topic,
        model_used: state.lastUsedModelName || "",
      }),
    });
    status.innerHTML = `Документ готов. <a class="link-btn" href="${result.download_url}">Скачать .docx</a>`;
    status.className = "hint ok";
  } catch (e) {
    status.textContent = "Ошибка: " + e.message;
    status.className = "hint error";
  } finally {
    document.getElementById("btnBuild").disabled = false;
  }
});

// ==================== AUTHORS CRUD TAB ====================

async function loadAuthorsTable() {
  state.authorsTableCache = await api("/authors");
  applyAuthorTableFilters();
}

document.getElementById("btnExportAuthors").addEventListener("click", () => {
  window.open(API + "/authors/export", "_blank");
});

document.getElementById("authorsImportInput").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const status = document.getElementById("authorsImportStatus");
  status.textContent = "Загружаем и разбираем файл...";
  status.className = "hint";
  const formData = new FormData();
  formData.append("file", file);
  try {
    const res = await fetch(API + "/authors/import", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    let msg = `Готово: добавлено ${data.created}, обновлено ${data.updated}`;
    if (data.skipped) msg += `, пропущено ${data.skipped}`;
    if (data.errors && data.errors.length) msg += `. Подробности: ${data.errors.join("; ")}`;
    status.textContent = msg;
    status.className = "hint " + (data.skipped ? "error" : "ok");
    await loadAuthorsTable();
    refreshAuthorsCache(); // чтобы новые/обновлённые авторы сразу были видны в автодополнении на "Новое предложение"
  } catch (err) {
    status.textContent = "Ошибка: " + err.message;
    status.className = "hint error";
  } finally {
    e.target.value = "";
  }
});

function applyAuthorTableFilters() {
  const globalQ = document.getElementById("authorSearch").value;
  const colFilters = {};
  document.querySelectorAll("#authorsTable [data-filter]").forEach((el) => {
    colFilters[el.dataset.filter] = el.value.trim().toLowerCase();
  });
  const rows = state.authorsTableCache.filter((a) => {
    if (!authorMatchesQuery(a, globalQ)) return false;
    return AUTHOR_FIELDS.every((f) => !colFilters[f] || (a[f] || "").toLowerCase().includes(colFilters[f]));
  });
  const tbody = document.querySelector("#authorsTable tbody");
  tbody.innerHTML =
    rows
      .map(
        (a) => `<tr data-id="${a.id}">
        <td data-label="Звание">${escapeHtml(a.rank)}</td>
        <td data-label="ФИО">${escapeHtml(a.full_name)}</td>
        <td data-label="Должность">${escapeHtml(a.position)}</td>
        <td data-label="Место службы">${escapeHtml(a.workplace)}</td>
        <td><div class="model-actions">
          <button class="link-btn" data-action="edit" data-id="${a.id}">изменить</button>
          <button class="link-btn" data-action="delete" data-id="${a.id}">удалить</button>
        </div></td>
      </tr>`
      )
      .join("") || `<tr><td colspan="5">${state.authorsTableCache.length ? "Ничего не найдено." : "Пока нет ни одного автора. Добавьте первого выше."}</td></tr>`;
}

document.getElementById("authorSearch").addEventListener("input", applyAuthorTableFilters);
document.querySelectorAll("#authorsTable [data-filter]").forEach((el) => el.addEventListener("input", applyAuthorTableFilters));

function setAuthorFormMode(author = null) {
  document.getElementById("a_id").value = author?.id || "";
  document.getElementById("a_rank").value = author?.rank || "";
  document.getElementById("a_name").value = author?.full_name || "";
  document.getElementById("a_position").value = author?.position || "";
  document.getElementById("a_workplace").value = author?.workplace || "";
  document.getElementById("authorFormTitle").textContent = author ? `Изменить автора: ${author.full_name}` : "Новый автор";
  document.getElementById("btnSaveAuthor").textContent = author ? "Сохранить изменения" : "Добавить";
  document.getElementById("btnCancelAuthorEdit").classList.toggle("hidden", !author);
  document.getElementById("authorFormStatus").textContent = "";
}

document.getElementById("btnCancelAuthorEdit").addEventListener("click", () => setAuthorFormMode(null));

document.getElementById("btnSaveAuthor").addEventListener("click", async () => {
  const id = document.getElementById("a_id").value;
  const rank = document.getElementById("a_rank").value.trim();
  const full_name = document.getElementById("a_name").value.trim();
  const position = document.getElementById("a_position").value.trim();
  const workplace = document.getElementById("a_workplace").value.trim();
  const statusEl = document.getElementById("authorFormStatus");
  if (!full_name) {
    statusEl.textContent = "Укажите ФИО автора.";
    statusEl.className = "hint error";
    return;
  }
  try {
    if (id) {
      await api("/authors/" + id, { method: "PUT", body: JSON.stringify({ rank, full_name, position, workplace }) });
    } else {
      await api("/authors", { method: "POST", body: JSON.stringify({ rank, full_name, position, workplace }) });
    }
    setAuthorFormMode(null);
    await loadAuthorsTable();
    refreshAuthorsCache(); // чтобы новый/изменённый автор сразу был виден в поиске на шаге 4
  } catch (e) {
    statusEl.textContent = "Ошибка: " + e.message;
    statusEl.className = "hint error";
  }
});

document.querySelector("#authorsTable tbody").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const id = btn.dataset.id;
  if (btn.dataset.action === "edit") {
    const author = state.authorsTableCache.find((a) => a.id === Number(id));
    if (author) {
      setAuthorFormMode(author);
      document.getElementById("authorFormTitle").scrollIntoView({ behavior: "smooth", block: "center" });
    }
  } else if (btn.dataset.action === "delete") {
    if (!confirm("Удалить автора?")) return;
    await api("/authors/" + id, { method: "DELETE" });
    loadAuthorsTable();
    refreshAuthorsCache();
  }
});

// ==================== HISTORY TAB ====================
async function loadHistory() {
  const proposals = await api("/proposals");
  const tbody = document.querySelector("#historyTable tbody");
  tbody.innerHTML = proposals
    .map(
      (p) => `<tr data-id="${p.id}">
        <td>${p.id}</td>
        <td>${escapeHtml(p.title)}</td>
        <td>${new Date(p.created_at + "Z").toLocaleDateString("ru-RU")}</td>
        <td>${escapeHtml(p.provider || "—")}</td>
        <td><a class="link-btn" href="/api/proposals/${p.id}/download">скачать</a></td>
        <td><button class="link-btn" data-action="delete-proposal" data-id="${p.id}">удалить</button></td>
      </tr>`
    )
    .join("") || `<tr><td colspan="6">Пока ничего не сгенерировано.</td></tr>`;
}

document.querySelector("#historyTable tbody").addEventListener("click", async (e) => {
  const btn = e.target.closest('button[data-action="delete-proposal"]');
  if (!btn) return;
  const row = btn.closest("tr");
  const title = row.children[1].textContent;
  if (!confirm(`Удалить предложение «${title}»? Файл .docx тоже будет удалён безвозвратно.`)) return;
  btn.textContent = "удаляем...";
  try {
    await api("/proposals/" + btn.dataset.id, { method: "DELETE" });
    loadHistory();
  } catch (e2) {
    alert("Ошибка: " + e2.message);
    btn.textContent = "удалить";
  }
});

// ==================== SETTINGS TAB: LLM MODELS ====================

function statusBadge(m) {
  if (!m.last_check_status) return `<span class="badge unknown">не проверено</span>`;
  if (m.last_check_status === "ok") return `<span class="badge ok" title="${escapeAttr(m.last_check_message)}">доступна</span>`;
  return `<span class="badge error" title="${escapeAttr(m.last_check_message)}">ошибка</span>`;
}

function openRouterModelUrl(m) {
  if (!m.model) return null;
  if (!/openrouter\.ai/i.test(m.base_url || "")) return null;
  return `https://openrouter.ai/${m.model.replace(/^\/+/, "")}`;
}

// Локальные модели (Ollama, LM Studio, свой сервер в локальной сети) обычно
// работают без ключа - для них «нет ключа» не повод для предупреждения.
function modelLikelyNeedsKey(m) {
  if (m.provider_type === "gemini") return true;
  if (m.provider_type === "ollama") return false;
  let host = "";
  try {
    host = new URL(m.base_url).hostname;
  } catch (_) {
    return true;
  }
  return !(
    host === "localhost" || host === "127.0.0.1" || host === "host.docker.internal" ||
    /^10\./.test(host) || /^192\.168\./.test(host) || /^172\.(1[6-9]|2\d|3[01])\./.test(host) ||
    host.endsWith(".local")
  );
}

function keyStateHtml(m) {
  if (m.api_key_set) return `<div class="key-state ok" title="Ключ сохранён на сервере (${escapeAttr(m.api_key)})">🔑 ключ сохранён</div>`;
  if (modelLikelyNeedsKey(m)) return `<div class="key-state missing" title="Для этого провайдера обычно нужен API-ключ">⚠ нет ключа</div>`;
  return "";
}

function applyModelFilters() {
  const q = document.getElementById("modelSearch").value.trim().toLowerCase();
  const colFilters = {};
  document.querySelectorAll("#modelsTable [data-filter]").forEach((el) => {
    colFilters[el.dataset.filter] = el.value;
  });
  const rows = state.modelsCache.filter((m) => {
    if (q && !(m.name.toLowerCase().includes(q) || m.model.toLowerCase().includes(q))) return false;
    if (colFilters.name && !m.name.toLowerCase().includes(colFilters.name.toLowerCase())) return false;
    if (colFilters.model && !m.model.toLowerCase().includes(colFilters.model.toLowerCase())) return false;
    if (colFilters.provider_type && m.provider_type !== colFilters.provider_type) return false;
    if (colFilters.is_paid !== "" && colFilters.is_paid !== undefined && String(m.is_paid ? 1 : 0) !== colFilters.is_paid) return false;
    if (colFilters.last_check_status) {
      const st = m.last_check_status || "unknown";
      if (st !== colFilters.last_check_status) return false;
    }
    if (colFilters.limit && limitStatus(m).key !== colFilters.limit) return false;
    return true;
  });

  const { field, dir } = state.modelSort;
  rows.sort((a, b) => {
    let av, bv;
    if (field === "enabled") {
      av = a.enabled ? 1 : 0;
      bv = b.enabled ? 1 : 0;
    } else {
      av = a.priority;
      bv = b.priority;
    }
    if (av !== bv) return dir === "asc" ? av - bv : bv - av;
    return a.priority - b.priority; // стабильный вторичный порядок
  });

  const tbody = document.querySelector("#modelsTable tbody");
  tbody.innerHTML =
    rows
      .map((m) => {
        const orUrl = openRouterModelUrl(m);
        const modelCell = orUrl
          ? `<a class="model-name-link" href="${escapeAttr(orUrl)}" target="_blank" rel="noopener" title="Открыть модель на openrouter.ai">${escapeHtml(m.model)}</a>`
          : escapeHtml(m.model);
        const typeShort = PROVIDER_TYPE_SHORT[m.provider_type] || m.provider_type;
        const typeFull = state.llmProviderTypeLabels[m.provider_type] || m.provider_type;
        const ls = limitStatus(m);
        return `<tr data-id="${m.id}" class="${m.enabled ? "" : "row-disabled"}">
        <td class="toggle-cell"><input type="checkbox" data-action="toggle" data-id="${m.id}" ${m.enabled ? "checked" : ""} title="Включена для использования"></td>
        <td>${escapeHtml(m.name)}${keyStateHtml(m)}</td>
        <td><span class="type-badge" title="${escapeAttr(typeFull)}">${escapeHtml(typeShort)}</span></td>
        <td>${modelCell}</td>
        <td><span class="badge ${m.is_paid ? "paid" : "free"}">${m.is_paid ? "платная" : "бесплатная"}</span></td>
        <td><span class="badge ${ls.cls}" title="${escapeAttr(m.last_check_message || "Проверьте кнопкой «проверить» или сгенерируйте документ этой моделью")}">${ls.label}</span></td>
        <td>${m.priority}</td>
        <td>${statusBadge(m)}</td>
        <td><div class="model-actions">
          <button class="link-btn" data-action="check" data-id="${m.id}">проверить</button>
          <button class="link-btn" data-action="edit" data-id="${m.id}">изменить</button>
          <button class="link-btn" data-action="delete" data-id="${m.id}">удалить</button>
        </div></td>
      </tr>`;
      })
      .join("") || `<tr><td colspan="9">${state.modelsCache.length ? "Ничего не найдено." : "Пока нет ни одной модели. Добавьте первую выше."}</td></tr>`;
}

async function loadModelsTable() {
  state.modelsCache = await api("/models");
  applyModelFilters();
}

document.getElementById("modelSearch").addEventListener("input", applyModelFilters);
document.querySelectorAll("#modelsTable [data-filter]").forEach((el) => el.addEventListener("input", applyModelFilters));

document.querySelectorAll("#modelsTable th.sortable").forEach((th) => {
  th.addEventListener("click", () => {
    const field = th.dataset.sort;
    if (state.modelSort.field === field) {
      state.modelSort.dir = state.modelSort.dir === "asc" ? "desc" : "asc";
    } else {
      state.modelSort = { field, dir: "asc" };
    }
    document.querySelectorAll("#modelsTable th.sortable").forEach((t) => t.classList.remove("sort-active"));
    th.classList.add("sort-active");
    applyModelFilters();
  });
});

async function loadProviderTypes() {
  state.llmProviderTypeLabels = await api("/models/provider-types");
  const sel = document.getElementById("m_provider_type");
  sel.innerHTML = Object.entries(state.llmProviderTypeLabels)
    .map(([key, label]) => `<option value="${key}">${escapeHtml(label)}</option>`)
    .join("");
  const filterSel = document.querySelector('#modelsTable [data-filter="provider_type"]');
  filterSel.innerHTML =
    `<option value="">все</option>` +
    Object.entries(state.llmProviderTypeLabels)
      .map(([key]) => `<option value="${key}">${escapeHtml(PROVIDER_TYPE_SHORT[key] || key)}</option>`)
      .join("");
}

function showModelForm(model = null) {
  document.getElementById("modelFormStatus").textContent = "";
  document.getElementById("modelDetailsTitle").textContent = model ? `Модель: ${model.name}` : "Новая модель";
  document.getElementById("m_id").value = model?.id || "";
  document.getElementById("m_name").value = model?.name || "";
  document.getElementById("m_provider_type").value = model?.provider_type || "openai_compat";
  document.getElementById("m_base_url").value = model?.base_url || "";
  document.getElementById("m_model").value = model?.model || "";
  // Поле API-ключа всегда начинается ПУСТЫМ, а не с реального значения:
  // GET /api/models больше не отдаёт ключ открытым текстом (см. бэкенд,
  // _mask_api_key) - сюда приходит уже маска вида "sk-…abc". Показываем её
  // только как подпись-напоминание, а в само поле не вписываем ничего, чтобы
  // случайно не отправить маску обратно как "новый ключ". apiKeyTouched
  // отслеживает, начал ли пользователь реально печатать новый ключ.
  const apiKeyInput = document.getElementById("m_api_key");
  apiKeyInput.value = "";
  apiKeyInput.dataset.clear = "0";
  const form = document.getElementById("modelForm");
  form.dataset.apiKeySet = model?.api_key_set ? "1" : "0";
  form.dataset.apiKeyMask = model?.api_key || "";
  form.dataset.keyDestination = model ? keyDestination(model.provider_type, model.base_url) : "";
  document.getElementById("m_name").dataset.fromCatalog = "0";
  renderApiKeyState();
  document.getElementById("m_priority").value = model?.priority ?? 100;
  document.getElementById("m_max_tokens").value = model?.max_tokens ?? 4000;
  document.getElementById("m_is_paid").checked = !!model?.is_paid;

  const logWrap = document.getElementById("modelLogWrap");
  if (model) {
    logWrap.classList.remove("hidden");
    loadModelLog(model.id);
  } else {
    logWrap.classList.add("hidden");
    document.getElementById("modelLogList").innerHTML = "";
  }

  document.getElementById("modelDetailsModal").classList.remove("hidden");
}

function closeModelDetailsModal() {
  closeCatalogModal();
  document.getElementById("modelDetailsModal").classList.add("hidden");
  state.modelLogOffset = 0;
  state.modelLogModelId = null;
}

document.getElementById("btnShowAddModel").addEventListener("click", () => showModelForm());
document.getElementById("btnCancelModel").addEventListener("click", closeModelDetailsModal);
document.getElementById("modelDetailsClose").addEventListener("click", closeModelDetailsModal);
document.getElementById("modelDetailsModal").addEventListener("click", (e) => {
  if (e.target.id === "modelDetailsModal") closeModelDetailsModal();
});
document.addEventListener("keydown", (e) => {
  // Esc при открытом поверх каталоге закрывает только каталог (см. обработчик
  // ниже), а не сразу оба окна вместе с несохранённой формой модели
  if (!document.getElementById("catalogModal").classList.contains("hidden")) return;
  if (e.key === "Escape" && !document.getElementById("modelDetailsModal").classList.contains("hidden")) {
    closeModelDetailsModal();
  }
});

// ---------- журнал событий модели (внутри той же модалки) ----------

const LOG_TYPE_LABELS = { check: "проверка", generation: "генерация" };
const LOG_PAGE_SIZE = 30;

function renderLogEntries(events, append, modelId) {
  const list = document.getElementById("modelLogList");
  if (!append) list.innerHTML = "";
  if (!events.length && !append) {
    list.innerHTML = `<div class="log-empty">Журнал пуст — здесь появятся записи после первой проверки или генерации этой моделью.</div>`;
    return;
  }
  const html = events
    .map((ev) => {
      const when = new Date(ev.created_at + "Z").toLocaleString("ru-RU");
      const typeLabel = LOG_TYPE_LABELS[ev.event_type] || ev.event_type;
      const downloadLink = ev.has_raw_response
        ? `<a class="link-btn log-entry-download" href="${API}/models/${modelId}/log/${ev.id}/raw" download title="Полный, ничем не обрезанный ответ модели">⬇ полный ответ</a>`
        : "";
      return `<div class="log-entry">
        <div class="log-entry-head">
          <span class="badge ${ev.status === "ok" ? "ok" : "error"}">${ev.status === "ok" ? "успех" : "ошибка"}</span>
          <span class="log-entry-type">${escapeHtml(typeLabel)}</span>
          <span class="log-entry-time">${when}</span>
        </div>
        <div class="log-entry-message">${escapeHtml(ev.message)}</div>
        ${downloadLink}
      </div>`;
    })
    .join("");
  list.insertAdjacentHTML("beforeend", html);
}

async function loadModelLog(modelId, offset = 0) {
  state.modelLogModelId = modelId;
  state.modelLogOffset = offset;
  const result = await api(`/models/${modelId}/log?limit=${LOG_PAGE_SIZE}&offset=${offset}`);
  renderLogEntries(result.events, offset > 0, modelId);
  const loadMoreBtn = document.getElementById("btnLoadMoreLog");
  loadMoreBtn.classList.toggle("hidden", offset + result.events.length >= result.total);
}

document.getElementById("btnLoadMoreLog").addEventListener("click", () => {
  if (state.modelLogModelId) loadModelLog(state.modelLogModelId, state.modelLogOffset + LOG_PAGE_SIZE);
});

document.getElementById("btnClearModelLog").addEventListener("click", async () => {
  if (!state.modelLogModelId) return;
  if (!confirm("Очистить журнал событий этой модели?")) return;
  await api(`/models/${state.modelLogModelId}/log`, { method: "DELETE" });
  loadModelLog(state.modelLogModelId);
});

// ---------- состояние API-ключа в форме модели ----------
// Сохранённый ключ в браузер не передаётся (только маска вида «sk-…b89»),
// поэтому поле ввода всегда пустое - а что ключ есть, показывают значок
// рядом с подписью, подсказка в самом поле и строка под ним.
// Пустое поле = «оставить как есть». Удалить ключ можно только явно, кнопкой
// «удалить сохранённый ключ»: раньше случайно напечатанный и стёртый символ
// при сохранении молча стирал ключ.
function renderApiKeyState() {
  const form = document.getElementById("modelForm");
  const input = document.getElementById("m_api_key");
  const badge = document.getElementById("m_api_key_state");
  const hint = document.getElementById("m_api_key_hint");
  const btn = document.getElementById("btnClearApiKey");
  const isSet = form.dataset.apiKeySet === "1";
  const mask = form.dataset.apiKeyMask || "";
  const typed = input.value.trim() !== "";
  const clearing = input.dataset.clear === "1";
  // ключ привязан к серверу, для которого его сохранили (так же проверяет и сервер)
  const movedToOtherServer = isSet && keyDestination(
    document.getElementById("m_provider_type").value, document.getElementById("m_base_url").value
  ) !== form.dataset.keyDestination;

  input.disabled = clearing;
  btn.classList.toggle("hidden", !isSet);
  btn.textContent = clearing ? "отменить удаление" : "удалить сохранённый ключ";
  if (movedToOtherServer && !typed && !clearing) {
    badge.className = "badge error";
    badge.textContent = "нужен заново";
    input.placeholder = "впишите ключ для нового адреса";
    hint.textContent =
      `Адрес изменён на другой сервер. Сохранённый ключ (${mask}) туда отправлен не будет - ` +
      "впишите ключ для нового адреса или удалите сохранённый.";
  } else if (clearing) {
    badge.className = "badge error";
    badge.textContent = "будет удалён";
    input.placeholder = "ключ будет удалён";
    hint.textContent = "Сохранённый ключ будет удалён при нажатии «Сохранить».";
  } else if (typed) {
    badge.className = "badge ok";
    badge.textContent = isSet ? "будет заменён" : "будет сохранён";
    hint.textContent = isSet
      ? `Новый ключ заменит сохранённый (${mask}) при нажатии «Сохранить».`
      : "Ключ будет сохранён при нажатии «Сохранить».";
  } else if (isSet) {
    badge.className = "badge ok";
    badge.textContent = "✓ сохранён";
    input.placeholder = `сохранён: ${mask} — впишите новый, чтобы заменить`;
    hint.textContent = `Ключ сохранён на сервере (${mask}). Само значение не показывается из соображений безопасности.`;
  } else {
    badge.className = "badge unknown";
    badge.textContent = "не задан";
    input.placeholder = "впишите ключ, если провайдер его требует";
    hint.textContent = "Локальным Ollama и LM Studio ключ обычно не нужен.";
  }
}

// Куда фактически уйдёт ключ: Gemini - всегда к API Google, остальные - на
// хост:порт из адреса (как _key_destination в backend/main.py)
function keyDestination(providerType, baseUrl) {
  if (providerType === "gemini") return "gemini";
  try {
    return new URL((baseUrl || "").trim()).host.toLowerCase();
  } catch (_) {
    return "";
  }
}

document.getElementById("m_api_key").addEventListener("input", renderApiKeyState);
document.getElementById("m_base_url").addEventListener("input", renderApiKeyState);
document.getElementById("m_provider_type").addEventListener("change", renderApiKeyState);
document.getElementById("btnClearApiKey").addEventListener("click", () => {
  const input = document.getElementById("m_api_key");
  input.dataset.clear = input.dataset.clear === "1" ? "0" : "1";
  if (input.dataset.clear === "1") input.value = "";
  renderApiKeyState();
});

document.getElementById("modelForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("m_id").value;
  const apiKeyInput = document.getElementById("m_api_key");
  // пробелы и переводы строки по краям - частый мусор при вставке ключа
  const typedKey = apiKeyInput.value.trim();
  const clearKey = apiKeyInput.dataset.clear === "1";
  // ключ меняется, только если вписан новый или явно нажато «удалить»;
  // пустое поле = оставить сохранённый как есть
  const apiKeyChanged = clearKey || typedKey !== "";
  const payload = {
    name: document.getElementById("m_name").value.trim(),
    provider_type: document.getElementById("m_provider_type").value,
    base_url: document.getElementById("m_base_url").value.trim(),
    model: document.getElementById("m_model").value.trim(),
    api_key: clearKey ? "" : typedKey,
    priority: Number(document.getElementById("m_priority").value) || 100,
    is_paid: document.getElementById("m_is_paid").checked,
    max_tokens: Number(document.getElementById("m_max_tokens").value) || 4000,
  };
  const statusEl = document.getElementById("modelFormStatus");
  if (!payload.name || !payload.model) {
    statusEl.textContent = "Укажите хотя бы название и модель.";
    statusEl.className = "hint error";
    return;
  }
  try {
    if (id) {
      const existing = state.modelsCache.find((m) => m.id === Number(id));
      // api_key_changed: false (поле не трогали) - значит api_key в payload
      // это просто маска/пустая строка из формы, и бэкенд обязан её
      // игнорировать, сохранив настоящий ключ как есть (см. ModelUpdateIn
      // в backend/main.py) - иначе сохранение любой другой правки (приоритет,
      // лимит токенов) затёрло бы реальный ключ маской.
      await api("/models/" + id, {
        method: "PUT",
        body: JSON.stringify({ ...payload, enabled: existing ? !!existing.enabled : true, api_key_changed: apiKeyChanged }),
      });
    } else {
      await api("/models", { method: "POST", body: JSON.stringify(payload) });
    }
    closeModelDetailsModal();
    await loadModelsTable();
    initProviderSelect();
  } catch (err) {
    statusEl.textContent = "Ошибка: " + err.message;
    statusEl.className = "hint error";
  }
});

// ==================== КАТАЛОГ МОДЕЛЕЙ (OpenRouter, Groq, Gemini, ..., Ollama, LM Studio) ====================
// Второе окно поверх формы модели: список моделей провайдера с поиском,
// фильтром бесплатные/платные и сортировкой. Список провайдеров и сами
// модели запрашивает сервер (см. backend/catalog.py) - так ключ API не уходит
// в браузер, а у OpenRouter популярность честно показывается только когда
// сервер её реально применил.

const catalogState = { sources: [], byId: {}, source: "openrouter", urls: {}, data: null, seq: 0, sortSource: null };

function sourceLabel(id) {
  return catalogState.byId[id]?.label || id;
}

async function ensureCatalogSources() {
  if (catalogState.sources.length) return;
  catalogState.sources = await api("/catalog/sources");
  catalogState.byId = Object.fromEntries(catalogState.sources.map((s) => [s.id, s]));
  const groups = [];
  for (const s of catalogState.sources) {
    let g = groups.find((x) => x.name === s.group);
    if (!g) groups.push((g = { name: s.group, items: [] }));
    g.items.push(s);
  }
  document.getElementById("catalogSource").innerHTML = groups
    .map((g) => `<optgroup label="${escapeAttr(g.name)}">${g.items.map((s) => `<option value="${s.id}">${escapeHtml(s.label)}</option>`).join("")}</optgroup>`)
    .join("");
}

function guessCatalogSource() {
  const type = document.getElementById("m_provider_type").value;
  const url = document.getElementById("m_base_url").value.trim().toLowerCase();
  if (type === "gemini") return "gemini";
  if (type === "ollama") return "ollama";
  const known = catalogState.sources.find((s) => s.host_hint && url.includes(s.host_hint));
  if (known) return known.id;
  if (url.includes(":1234") || url.includes("lmstudio")) return "lmstudio";
  if (url) return "custom";
  return "openrouter";
}

function formUrlForSource(source) {
  // адрес из формы подставляем, только если он явно про этот же источник -
  // иначе (например, в форме адрес Groq, а открыт OpenRouter) берём адрес по умолчанию
  const type = document.getElementById("m_provider_type").value;
  const url = document.getElementById("m_base_url").value.trim();
  const low = url.toLowerCase();
  const s = catalogState.byId[source];
  if (!url || !s || source === "gemini") return null;
  if (source === "ollama") return type === "ollama" ? url : null;
  if (s.host_hint) return low.includes(s.host_hint) ? url : null;
  if (source === "lmstudio") return type === "openai_compat" && (low.includes(":1234") || low.includes("lmstudio")) ? url : null;
  // «другой сервер» - адрес из формы, если он не принадлежит ни одному известному провайдеру
  const known = catalogState.sources.some((x) => x.host_hint && low.includes(x.host_hint));
  return type === "openai_compat" && !known ? url : null;
}

async function openCatalogModal() {
  document.getElementById("catalogModal").classList.remove("hidden");
  try {
    await ensureCatalogSources();
  } catch (e) {
    const status = document.getElementById("catalogStatus");
    status.textContent = "Ошибка: " + e.message;
    status.className = "hint error";
    return;
  }
  const source = guessCatalogSource();
  catalogState.urls = Object.fromEntries(catalogState.sources.map((s) => [s.id, s.default_url]));
  const fromForm = formUrlForSource(source);
  if (fromForm) catalogState.urls[source] = fromForm;
  document.getElementById("catalogSearch").value = "";
  document.getElementById("catalogPriceFilter").value = "all";
  setCatalogSource(source);
  setTimeout(() => document.getElementById("catalogSearch").focus(), 0);
}

function closeCatalogModal() {
  document.getElementById("catalogModal").classList.add("hidden");
  catalogState.seq++; // ответ уже летящего запроса больше не нужен
}

function renderCatalogSourceInfo() {
  const s = catalogState.byId[catalogState.source];
  const link = s.key_url ? ` — <a href="${escapeAttr(s.key_url)}" target="_blank" rel="noopener">получить ключ ↗</a>` : "";
  const parts = [];
  if (s.needs_key) {
    parts.push(`Нужен API-ключ${link}. Берётся из поля «API-ключ» формы или из уже сохранённой модели ${escapeHtml(s.label)}`);
  } else if (s.id === "openrouter") {
    parts.push(`Ключ не обязателен, но с ним доступна сортировка по популярности${link}`);
  } else if (s.id === "custom") {
    parts.push("Адрес обычно заканчивается на /v1 (OpenAI, Fireworks, Hugging Face, свой vLLM). Ключ - из поля «API-ключ» формы, если сервер его требует");
  }
  if (s.price_note) parts.push(`Тариф: ${escapeHtml(s.price_note)}`);
  else if (s.pricing_mode === "per_model") parts.push("Цена указана по каждой модели");
  document.getElementById("catalogSourceInfo").innerHTML = parts.join(" · ");
}

function setCatalogSource(source) {
  catalogState.source = source;
  const s = catalogState.byId[source];
  document.getElementById("catalogSource").value = source;
  const urlInput = document.getElementById("catalogBaseUrl");
  urlInput.value = catalogState.urls[source] || "";
  // драйвер Gemini всегда обращается к API Google напрямую - менять адрес бессмысленно
  urlInput.disabled = source === "gemini";
  urlInput.title = source === "gemini" ? "Адрес Google Gemini фиксированный: программа обращается к API Google напрямую" : "";
  urlInput.placeholder = source === "custom" ? "Например: https://api.openai.com/v1" : "Адрес сервера";
  // цена известна по каждой модели только у части провайдеров - для остальных фильтр бессмысленен
  const priceFilter = document.getElementById("catalogPriceFilter");
  priceFilter.disabled = s.pricing_mode !== "per_model";
  if (priceFilter.disabled) priceFilter.value = "all";
  document.getElementById("catalogPriceHeader").innerHTML =
    s.pricing_mode === "per_model" ? `Цена за 1 млн токенов<br><span class="muted">вход / выход</span>` : "Тариф";
  // пока список не загружен, в сортировке нет пунктов - не даём ей схлопнуться
  const sortSel = document.getElementById("catalogSort");
  if (catalogState.sortSource !== source) {
    sortSel.innerHTML = `<option value="default">Как в списке</option>`;
    catalogState.sortSource = null;
  }
  renderCatalogSourceInfo();
  if (source === "custom" && !urlInput.value.trim()) {
    catalogState.seq++;
    catalogState.data = null;
    document.querySelector("#catalogTable tbody").innerHTML = "";
    const status = document.getElementById("catalogStatus");
    status.textContent = "Впишите адрес сервера и нажмите «⟳ Обновить».";
    status.className = "hint";
    urlInput.focus();
    return;
  }
  loadCatalog(false);
}

async function loadCatalog(refresh) {
  const seq = ++catalogState.seq;
  const source = catalogState.source;
  const status = document.getElementById("catalogStatus");
  const tbody = document.querySelector("#catalogTable tbody");
  catalogState.data = null;
  status.textContent = `Загружаем список моделей ${sourceLabel(source)}...`;
  status.className = "hint";
  tbody.innerHTML = `<tr><td colspan="5" class="muted">Загрузка...</td></tr>`;

  const catalogUrl = document.getElementById("catalogBaseUrl").value.trim();
  const formKey = formKeyForCatalog(source, catalogUrl);
  const body = {
    source,
    base_url: catalogUrl,
    refresh,
    model_id: Number(document.getElementById("m_id").value) || null,
    // только что вписанный (ещё не сохранённый) ключ - и только если он
    // относится к этому же серверу; иначе сервер сам возьмёт ключ уже
    // сохранённой модели с тем же адресом (если такая есть)
    api_key: formKey.key,
  };
  try {
    const data = await api("/catalog", { method: "POST", body: JSON.stringify(body) });
    if (seq !== catalogState.seq) return; // пользователь уже переключил источник
    catalogState.data = data;
    catalogState.urls[source] = data.base_url;
    buildCatalogSortOptions(data);
    renderCatalogTable();
  } catch (e) {
    if (seq !== catalogState.seq) return;
    status.textContent = "Ошибка: " + e.message +
      (formKey.withheld ? " (Ключ из формы не отправлен: он вписан для другого адреса.)" : "");
    status.className = "hint error";
    tbody.innerHTML = "";
  }
}

// Ключ из формы отправляем в каталог, только если он для этого же сервера
// (или адрес в форме ещё не выбран - новая модель): иначе, например, ключ
// Groq из формы ушёл бы в Together при просмотре его каталога.
function formKeyForCatalog(source, catalogUrl) {
  const input = document.getElementById("m_api_key");
  const key = input.dataset.clear === "1" ? "" : input.value.trim();
  if (!key) return { key: "", withheld: false };
  const type = document.getElementById("m_provider_type").value;
  const formUrl = document.getElementById("m_base_url").value.trim();
  const sourceType = catalogState.byId[source]?.provider_type;
  let fits;
  if (source === "gemini") fits = type === "gemini" || !formUrl;
  else if (!formUrl && type !== "gemini") fits = true;
  else fits = type === sourceType && sameKeyServer(formUrl, catalogUrl);
  return fits ? { key, withheld: false } : { key: "", withheld: true };
}

function sameKeyServer(a, b) {
  const norm = (u) => {
    const h = keyDestination("openai_compat", u);
    return h === "api.together.xyz" ? "api.together.ai" : h;
  };
  return norm(a) !== "" && norm(a) === norm(b);
}

function buildCatalogSortOptions(data) {
  const sel = document.getElementById("catalogSort");
  const prev = sel.value;
  const opts = [];
  // сортировки предлагаем только по тем полям, которые провайдер реально отдал:
  // популярность - только OpenRouter (и только если сервер её применил),
  // цена - только где она известна по каждой модели
  const hasField = (f) => data.models.some((m) => m[f] != null);
  if (data.source === "openrouter") {
    if (data.popularity_available) opts.push(["popular", "По популярности"]);
    else opts.push(["popular", "По популярности (нужен ключ OpenRouter)", true]);
    opts.push(["default", "Как в OpenRouter"]);
  } else {
    opts.push(["default", "Как в списке"]);
  }
  if (hasField("created")) opts.push(["newest", data.source === "ollama" ? "Недавно скачанные" : "Сначала новые"]);
  if (data.pricing_mode === "per_model") opts.push(["price", "Сначала дешёвые"]);
  if (hasField("context_length")) opts.push(["context", "Больше контекст"]);
  opts.push(["name", "По названию"]);
  sel.innerHTML = opts
    .map(([v, label, disabled]) => `<option value="${v}"${disabled ? " disabled" : ""}>${escapeHtml(label)}</option>`)
    .join("");
  const usable = opts.filter((o) => !o[2]).map((o) => o[0]);
  // выбор сортировки сохраняем только при обновлении того же источника; при
  // переключении (например, Ollama -> OpenRouter) начинаем с лучшей доступной -
  // иначе «Как в списке» от Ollama превращалось в «Как в OpenRouter» вместо популярности
  const sameSource = catalogState.sortSource === data.source;
  sel.value = sameSource && usable.includes(prev) ? prev : usable[0];
  catalogState.sortSource = data.source;
}

function catalogPriceKey(m) {
  if (m.is_free) return 0;
  if (m.price_in == null) return 1e12; // цена неизвестна - в конец списка
  return (m.price_in + (m.price_out ?? m.price_in)) / 2;
}

const CATALOG_SORTERS = {
  popular: (a, b) => (a.popularity_rank ?? 1e9) - (b.popularity_rank ?? 1e9),
  default: (a, b) => a.default_order - b.default_order,
  newest: (a, b) => (b.created ?? 0) - (a.created ?? 0),
  price: (a, b) => catalogPriceKey(a) - catalogPriceKey(b),
  context: (a, b) => (b.context_length ?? 0) - (a.context_length ?? 0),
  name: (a, b) => a.name.localeCompare(b.name, "ru"),
};

function fmtTokens(n) {
  if (!n) return "—";
  if (n >= 1_000_000) return `${+(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}K`;
  return String(n);
}

function fmtPrice(v) {
  if (v == null) return "?";
  if (v === 0) return "$0";
  return "$" + (v >= 1 ? +v.toFixed(2) : +v.toPrecision(2));
}

function renderCatalogTable() {
  const data = catalogState.data;
  if (!data) return;
  const q = document.getElementById("catalogSearch").value.trim().toLowerCase();
  const price = document.getElementById("catalogPriceFilter").value;
  const sort = document.getElementById("catalogSort").value;
  const perModelPrice = data.pricing_mode === "per_model";

  const rows = data.models
    .filter((m) => !q || m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
    .filter((m) => price === "all" || (price === "free" ? m.is_free : !m.is_free))
    .sort(CATALOG_SORTERS[sort] || CATALOG_SORTERS.default);

  const tbody = document.querySelector("#catalogTable tbody");
  tbody.innerHTML =
    rows
      .map((m) => {
        const badges = [];
        if (m.is_free && perModelPrice) badges.push(`<span class="badge free">бесплатно</span>`);
        if (m.is_reasoning) {
          badges.push(`<span class="badge reasoning" title="Часть лимита токенов уходит на внутренние рассуждения - рекомендуемый «Максимум токенов ответа» для неё увеличен">рассуждающая</span>`);
        }
        if (m.already_added_as) badges.push(`<span class="badge added" title="Уже есть в списке нейросетей">✓ уже добавлена: ${escapeHtml(m.already_added_as)}</span>`);
        if (m.expires) badges.push(`<span class="badge expiring" title="Провайдер снимает модель с этой даты">снимается ${escapeHtml(String(m.expires).slice(0, 10))}</span>`);
        let priceCell;
        if (!perModelPrice) priceCell = `<span class="muted">${escapeHtml(data.price_note || "—")}</span>`;
        else if (m.is_free) priceCell = "бесплатно";
        else if (m.price_in == null) {
          priceCell = data.source === "openrouter"
            ? `<span class="muted" title="Цена зависит от того, какой модели OpenRouter передаст запрос">зависит от маршрута</span>`
            : `<span class="muted">не указана</span>`;
        } else priceCell = `${fmtPrice(m.price_in)} / ${fmtPrice(m.price_out)}`;
        const showName = m.name && m.name !== m.id;
        return `<tr data-id="${escapeAttr(m.id)}" title="${escapeAttr(m.description || "")}">
          <td class="cat-rank">${m.popularity_rank ?? ""}</td>
          <td>
            ${showName ? `<div class="cat-name">${escapeHtml(m.name)}</div>` : ""}
            <div class="${showName ? "cat-id" : "cat-name"}">${escapeHtml(m.id)}</div>
            ${m.details ? `<div class="cat-meta">${escapeHtml(m.details)}</div>` : ""}
            ${badges.length ? `<div class="cat-badges">${badges.join("")}</div>` : ""}
          </td>
          <td class="cat-num">${fmtTokens(m.context_length)}</td>
          <td class="cat-num">${fmtTokens(m.max_output)}</td>
          <td class="cat-num">${priceCell}</td>
        </tr>`;
      })
      .join("") || `<tr><td colspan="5" class="muted">${data.models.length ? "Ничего не найдено - измените поиск или фильтр." : "Список пуст."}</td></tr>`;

  const status = document.getElementById("catalogStatus");
  const parts = [`Показано ${rows.length} из ${data.models.length}`];
  if (data.from_cache) {
    parts.push(`список из кэша от ${new Date(data.fetched_at + "Z").toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })} (⟳ Обновить - загрузить заново)`);
  }
  status.textContent = parts.join(" · ") + (data.warnings.length ? ". ⚠ " + data.warnings.join(" ") : "");
  status.className = "hint";
}

function fillModelFormFromCatalog(m) {
  const source = catalogState.source;
  document.getElementById("m_provider_type").value = m.provider_type;
  document.getElementById("m_base_url").value = m.base_url;
  document.getElementById("m_model").value = m.id;
  const nameInput = document.getElementById("m_name");
  // название подставляем, только если человек не вписал своё (или оно само
  // было подставлено из каталога при прошлом выборе)
  const info = catalogState.byId[source];
  const label = sourceLabel(source);
  if (!nameInput.value.trim() || nameInput.dataset.fromCatalog === "1") {
    nameInput.value = info.pricing_mode === "local" ? `${label}: ${m.name}` : `${m.name} (${label})`;
    nameInput.dataset.fromCatalog = "1";
  }
  document.getElementById("m_max_tokens").value = m.suggested_max_tokens;
  // тариф неизвестен (свой сервер) - галочку «Платная» не трогаем
  if (m.is_free === true || m.is_free === false) document.getElementById("m_is_paid").checked = !m.is_free;

  const apiKeyInput = document.getElementById("m_api_key");
  const form = document.getElementById("modelForm");
  // сохранённый ключ годится, только если модель осталась на том же сервере
  const savedKeyUsable = form.dataset.apiKeySet === "1" && apiKeyInput.dataset.clear !== "1" &&
    keyDestination(m.provider_type, m.base_url) === form.dataset.keyDestination;
  const hasKey = savedKeyUsable || apiKeyInput.value.trim() !== "";
  // ключ хранится у каждой модели свой: даже если каталог загрузился с ключом
  // другой сохранённой модели, новой модели его нужно вписать (можно тот же)
  const needsKey = info.needs_key || source === "openrouter";
  const msg = [`Заполнено из каталога ${label}: «${m.id}».`];
  if (needsKey && !hasKey) {
    msg.push(`Впишите API-ключ ${label}${info.key_url ? ` (выдают на ${info.key_url.replace(/^https?:\/\//, "")})` : ""} - можно тот же, что у уже сохранённой модели этого провайдера.`);
  }
  if (m.is_reasoning) msg.push(`Это рассуждающая модель - «Максимум токенов ответа» сразу увеличен до ${m.suggested_max_tokens}.`);
  if (m.already_added_as) msg.push(`Внимание: такая модель уже есть в списке («${m.already_added_as}») - сохранение создаст вторую копию.`);
  const status = document.getElementById("modelFormStatus");
  status.textContent = msg.join(" ");
  status.className = "hint " + (m.already_added_as ? "error" : "ok");

  closeCatalogModal();
  renderApiKeyState(); // адрес мог смениться на другой сервер - значок ключа должен это показать
  (needsKey && !hasKey ? apiKeyInput : nameInput).focus();
}

document.getElementById("btnOpenCatalog").addEventListener("click", openCatalogModal);
document.getElementById("catalogClose").addEventListener("click", closeCatalogModal);
document.getElementById("catalogModal").addEventListener("click", (e) => {
  if (e.target.id === "catalogModal") closeCatalogModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !document.getElementById("catalogModal").classList.contains("hidden")) {
    closeCatalogModal();
  }
});
document.getElementById("catalogSource").addEventListener("change", (e) => {
  // запоминаем отредактированный адрес прежнего источника - при возврате он сохранится
  const prevUrl = document.getElementById("catalogBaseUrl").value.trim();
  if (prevUrl) catalogState.urls[catalogState.source] = prevUrl;
  setCatalogSource(e.target.value);
});
document.getElementById("catalogReload").addEventListener("click", () => loadCatalog(true));
document.getElementById("catalogBaseUrl").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    loadCatalog(true);
  }
});
document.getElementById("catalogSearch").addEventListener("input", renderCatalogTable);
document.getElementById("catalogPriceFilter").addEventListener("change", renderCatalogTable);
document.getElementById("catalogSort").addEventListener("change", renderCatalogTable);
document.querySelector("#catalogTable tbody").addEventListener("click", (e) => {
  const row = e.target.closest("tr[data-id]");
  if (!row || !catalogState.data) return;
  const m = catalogState.data.models.find((x) => x.id === row.dataset.id);
  if (m) fillModelFormFromCatalog(m);
});
document.getElementById("m_name").addEventListener("input", (e) => {
  e.target.dataset.fromCatalog = "0"; // человек правит название сам - больше не перезаписываем
});

document.querySelector("#modelsTable tbody").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const id = btn.dataset.id;
  const model = state.modelsCache.find((m) => m.id === Number(id));

  if (btn.dataset.action === "edit") {
    showModelForm(model);
  } else if (btn.dataset.action === "delete") {
    if (!confirm(`Удалить модель «${model.name}»?`)) return;
    await api("/models/" + id, { method: "DELETE" });
    await loadModelsTable();
    initProviderSelect();
  } else if (btn.dataset.action === "check") {
    btn.textContent = "проверяем...";
    try {
      await api(`/models/${id}/check`, { method: "POST" });
    } catch (_) {
      // результат (в т.ч. ошибка) уже сохранён на бэкенде в last_check_*
    }
    await loadModelsTable();
    initProviderSelect(); // отметка лимита в выпадающем списке "Нейросеть" тоже могла измениться
  }
});

document.querySelector("#modelsTable tbody").addEventListener("change", async (e) => {
  if (e.target.dataset.action !== "toggle") return;
  const id = e.target.dataset.id;
  const model = state.modelsCache.find((m) => m.id === Number(id));
  if (!model) return;
  await api("/models/" + id, {
    method: "PUT",
    body: JSON.stringify({
      name: model.name,
      provider_type: model.provider_type,
      base_url: model.base_url,
      model: model.model,
      api_key: model.api_key, // маска из кэша - бэкенд её проигнорирует, см. api_key_changed
      api_key_changed: false, // только переключаем "включена/выключена" - настоящий ключ не трогаем
      priority: model.priority,
      is_paid: !!model.is_paid,
      max_tokens: model.max_tokens,
      enabled: e.target.checked,
    }),
  });
  await loadModelsTable();
  initProviderSelect();
});

document.getElementById("btnPingAll").addEventListener("click", async () => {
  const btn = document.getElementById("btnPingAll");
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = "⟳ Проверяем все...";
  try {
    await api("/models/check-all", { method: "POST" });
    await loadModelsTable();
    initProviderSelect();
  } catch (e) {
    alert("Ошибка при проверке моделей: " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
});

// ---------- сохранённые адреса API (автокомплит по образцу поиска авторов) ----------

async function loadEndpoints() {
  state.savedEndpoints = await api("/base-urls");

  const list = document.getElementById("endpointsList");
  list.innerHTML =
    state.savedEndpoints
      .map(
        (e) => `<div class="endpoint-row" data-id="${e.id}">
          <span class="e-label">${escapeHtml(e.label)}</span>
          <span class="e-url">${escapeHtml(e.url)}</span>
          <button class="link-btn" data-action="delete-endpoint" data-id="${e.id}">удалить</button>
        </div>`
      )
      .join("") || `<p class="hint">Пока нет сохранённых адресов.</p>`;
}

function renderBaseUrlSuggestions() {
  const input = document.getElementById("m_base_url");
  const box = document.getElementById("baseUrlSuggest");
  const q = input.value.trim().toLowerCase();
  const matches = state.savedEndpoints.filter(
    (e) => !q || e.label.toLowerCase().includes(q) || e.url.toLowerCase().includes(q)
  );
  if (!matches.length) {
    box.innerHTML = `<div class="suggest-empty">Совпадений нет — впишите новый адрес и сохраните его кнопкой 💾</div>`;
  } else {
    box.innerHTML = matches
      .map(
        (e) => `<div class="suggest-item" data-url="${escapeAttr(e.url)}">
          <div class="s-name">${escapeHtml(e.label)}</div>
          <div class="s-meta">${escapeHtml(e.url)}</div>
        </div>`
      )
      .join("");
  }
  box.classList.remove("hidden");
}

document.getElementById("m_base_url").addEventListener("input", renderBaseUrlSuggestions);
document.getElementById("m_base_url").addEventListener("focus", renderBaseUrlSuggestions);
document.getElementById("baseUrlSuggest").addEventListener("mousedown", (e) => {
  const item = e.target.closest(".suggest-item");
  if (!item) return;
  e.preventDefault(); // см. комментарий у пикера подписанта - тот же баг с повторным фокусом
  document.getElementById("m_base_url").value = item.dataset.url;
  document.getElementById("baseUrlSuggest").classList.add("hidden");
});
document.addEventListener("click", (e) => {
  if (e.target.closest(".autocomplete-wrap")) return;
  document.getElementById("baseUrlSuggest")?.classList.add("hidden");
});

document.getElementById("btnSaveBaseUrl").addEventListener("click", async () => {
  const url = document.getElementById("m_base_url").value.trim();
  if (!url) {
    alert("Сначала впишите адрес в поле выше.");
    return;
  }
  const label = prompt("Название для этого адреса (для себя):", url);
  if (label === null) return;
  await api("/base-urls", { method: "POST", body: JSON.stringify({ label: label.trim() || url, url }) });
  loadEndpoints();
});

document.getElementById("endpointsList").addEventListener("click", async (e) => {
  const btn = e.target.closest('button[data-action="delete-endpoint"]');
  if (!btn) return;
  await api("/base-urls/" + btn.dataset.id, { method: "DELETE" });
  loadEndpoints();
});

// ---------- подписанты (сохранённые ФИО, формат «И.Фамилия») ----------

async function loadSignatoriesList() {
  const list = document.getElementById("signatoriesList");
  const signatories = await api("/signatories");
  list.innerHTML =
    signatories
      .map(
        (s) => `<div class="endpoint-row" data-id="${s.id}">
          <span class="e-label">${escapeHtml(s.full_name)}</span>
          <span class="e-url"></span>
          <button class="link-btn" data-action="delete-signatory" data-id="${s.id}">удалить</button>
        </div>`
      )
      .join("") || `<p class="hint">Пока нет сохранённых подписантов.</p>`;
  return signatories;
}

document.getElementById("btnAddSignatory").addEventListener("click", async () => {
  const input = document.getElementById("newSignatoryInput");
  const name = input.value.trim();
  if (!name) return;
  await api("/signatories", { method: "POST", body: JSON.stringify({ full_name: name }) });
  input.value = "";
  await loadSignatoriesList();
  await refreshSignatoriesCache(); // подтягиваем свежий список сразу и для "Новое предложение"
});

document.getElementById("newSignatoryInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("btnAddSignatory").click();
});

document.getElementById("signatoriesList").addEventListener("click", async (e) => {
  const btn = e.target.closest('button[data-action="delete-signatory"]');
  if (!btn) return;
  await api("/signatories/" + btn.dataset.id, { method: "DELETE" });
  await loadSignatoriesList();
  await refreshSignatoriesCache();
});

async function loadSettings() {
  await loadProviderTypes();
  await loadModelsTable();
  await loadEndpoints();
  await loadSignatoriesList();
  await loadTemplateStatus();
  await loadStats();
}

// ---------- статистика по моделям (идея аудита №5) ----------
async function loadStats() {
  let rows;
  try {
    rows = await api("/stats");
  } catch (_) {
    rows = [];
  }
  const tbody = document.querySelector("#statsTable tbody");
  tbody.innerHTML =
    rows
      .map((r) => {
        const rate = r.gen_success_rate === null ? "—" : `${r.gen_success_rate}%`;
        const typeShort = PROVIDER_TYPE_SHORT[r.provider_type] || r.provider_type;
        return `<tr class="${r.enabled ? "" : "row-disabled"}">
          <td>${escapeHtml(r.name)}</td>
          <td>${escapeHtml(typeShort)}</td>
          <td>${r.gen_ok}</td>
          <td>${r.gen_error}</td>
          <td>${rate}</td>
          <td>${r.check_ok} / ${r.check_error}</td>
        </tr>`;
      })
      .join("") || `<tr><td colspan="6">Пока нет данных - статистика появится после первых проверок/генераций.</td></tr>`;
}

// ---------- шаблон документа ----------

async function loadTemplateStatus() {
  const s = await api("/template/status");
  const box = document.getElementById("templateStatusBox");
  const sizeKb = (s.size_bytes / 1024).toFixed(1);
  const when = new Date(s.modified_at).toLocaleString("ru-RU");
  box.innerHTML = s.custom_active
    ? `<span class="badge ok">пользовательский</span> загружен ${when}, ${sizeKb} КБ`
    : `<span class="badge unknown">встроенный</span> (по умолчанию), ${sizeKb} КБ`;
  document.getElementById("btnResetTemplate").classList.toggle("hidden", !s.custom_active);
}

document.getElementById("templateFileInput").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const status = document.getElementById("templateUploadStatus");
  status.textContent = "Загружаем и проверяем шаблон (пробная сборка документа)...";
  status.className = "hint";
  const formData = new FormData();
  formData.append("file", file);
  try {
    const res = await fetch(API + "/template", { method: "POST", body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    status.textContent = data.message || "Шаблон загружен и активирован.";
    status.className = "hint ok";
    await loadTemplateStatus();
  } catch (err) {
    status.textContent = "Шаблон отклонён: " + err.message;
    status.className = "hint error";
  } finally {
    e.target.value = "";
  }
});

document.getElementById("btnDownloadTemplate").addEventListener("click", () => {
  window.open(API + "/template/download", "_blank");
});

document.getElementById("btnResetTemplate").addEventListener("click", async () => {
  if (!confirm("Вернуть встроенный шаблон по умолчанию? Загруженный вами файл будет удалён с сервера.")) return;
  await api("/template", { method: "DELETE" });
  document.getElementById("templateUploadStatus").textContent = "Возвращён встроенный шаблон.";
  document.getElementById("templateUploadStatus").className = "hint ok";
  await loadTemplateStatus();
});

// ==================== О программе ====================
function openAboutModal() {
  document.getElementById("aboutVersion").textContent = APP_VERSION;
  document.getElementById("aboutModal").classList.remove("hidden");
}
function closeAboutModal() {
  document.getElementById("aboutModal").classList.add("hidden");
}
document.getElementById("footerYear").textContent = String(new Date().getFullYear());
document.getElementById("btnAbout").addEventListener("click", openAboutModal);
document.getElementById("aboutClose").addEventListener("click", closeAboutModal);
document.getElementById("aboutModal").addEventListener("click", (e) => {
  if (e.target.id === "aboutModal") closeAboutModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !document.getElementById("aboutModal").classList.contains("hidden")) closeAboutModal();
});

// ==================== проверка версии сервера ====================
// Если интерфейс уже новый, а сервер ещё не перезапущен после обновления,
// новые функции падают с невнятным "Эндпоинт ... не существует". Сразу и
// прямо говорим, в чём дело и что сделать.
async function checkBackendVersion() {
  let serverVersion = null;
  try {
    const res = await fetch(API + "/health", { cache: "no-store" });
    if (!res.ok) return; // сервер недоступен - это другая проблема, её покажут сами запросы
    serverVersion = (await res.json()).version || null;
  } catch (_) {
    return;
  }
  if (serverVersion === APP_VERSION) return;
  const banner = document.getElementById("versionBanner");
  banner.innerHTML =
    `<strong>Сервер программы работает на старой версии кода</strong> ` +
    `(сервер: ${escapeHtml(serverVersion || "до 2026.10.03")}, интерфейс: ${escapeHtml(APP_VERSION)}). ` +
    `Файлы обновлены, но сервер не перезапущен — новые функции (например, каталог моделей) будут выдавать ошибки. ` +
    `Перезапустите сервер: в Docker — <code>docker compose up -d --build</code>, ` +
    `без Docker — остановите и заново запустите uvicorn. Затем обновите страницу.`;
  banner.classList.remove("hidden");
}
checkBackendVersion();

// ==================== utils ====================
function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(s) {
  return escapeHtml(s);
}
