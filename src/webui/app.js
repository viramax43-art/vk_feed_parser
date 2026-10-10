"use strict";

/* Панель массового запуска: опрашивает /api/* и перерисовывает таблицы. */

const TABS = {
  run: {
    title: "Прогон",
    hint: "Выберите период и нажмите «Старт». «Стоп» — как Ctrl+C: прогресс сохраняется, следующий запуск продолжит с того же места.",
  },
  accounts: {
    title: "Аккаунты",
    hint: "Статусы пишет движок: работает / отлёжка / мёртв. «нет сессии» — нажмите «Войти в VK» в строке: откроется браузер, код из SMS вводится там же. Прокси берётся из строки аккаунта или из proxies.txt.",
  },
  tags: {
    title: "Теги",
    hint: "Один тег на строку, решётка необязательна. Строки с // и «# » — комментарии.",
  },
  events: {
    title: "События",
    hint: "Капчи, ошибки токена и пустые выдачи — последние 40 записей из базы.",
  },
};

const MODES = {
  tags: {
    title: "Файл тегов — data/tags.txt",
    hint: "Один тег на строку. Пустые строки, «//» и «# » — не теги.",
    save: (text) => putJson("/api/files/tags", { content: text }),
    after: () => loadTags(),
  },
  accounts: {
    title: "Файл аккаунтов — data/accounts.txt",
    hint: "Формат строки: телефон;пароль;2FA;прокси. 2FA и прокси необязательны.",
    save: (text) => putJson("/api/files/accounts", { content: text }),
    after: () => loadAccounts(),
  },
  addAccounts: {
    title: "Добавить аккаунты",
    hint: "Строки допишутся в конец data/accounts.txt и подхватятся при старте.",
    save: (text) => postJson("/api/accounts/add", { content: text }),
    after: () => loadAccounts(),
  },
};

const state = { tab: "run", logAfter: 0, running: false, mode: null, busy: false };

const $ = (id) => document.getElementById(id);

async function call(path, options) {
  const response = await fetch(path, options);
  const text = await response.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (err) {
      throw new Error(`${path}: ${text.slice(0, 200)}`);
    }
  }
  if (!response.ok || data.ok === false) {
    throw new Error(data.message || `${response.status} ${response.statusText}`);
  }
  return data;
}

const getJson = (path) => call(path);
const postJson = (path, body) =>
  call(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
const putJson = (path, body) =>
  call(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });

function toast(message, bad = false) {
  const box = $("toast");
  box.textContent = message;
  box.classList.toggle("bad", Boolean(bad));
  box.classList.remove("hidden");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => box.classList.add("hidden"), 4500);
}

function escapeHtml(value) {
  return String(value === undefined || value === null ? "" : value).replace(
    /[&<>"']/g,
    (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch],
  );
}

/* ------------------------------------------------------------------ таблицы */
function renderTable(table, heads, rows) {
  const head = heads.map((title) => `<th>${escapeHtml(title)}</th>`).join("");
  const body = rows.length
    ? rows
        .map(
          (row) =>
            `<tr${row.cls ? ` class="${row.cls}"` : ""}>${row.cells
              .map((cell) => `<td>${cell}</td>`)
              .join("")}</tr>`,
        )
        .join("")
    : `<tr><td class="empty" colspan="${heads.length}">пока пусто</td></tr>`;
  table.innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>`;
}

/* --------------------------------------------------------------------- меню */
function showTab(key) {
  state.tab = key;
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === key);
  });
  document.querySelectorAll(".page").forEach((page) => {
    page.classList.toggle("active", page.id === "page-" + key);
  });
  $("page-title").textContent = TABS[key].title;
  $("page-hint").textContent = TABS[key].hint;
  if (key === "accounts") void loadAccounts();
  if (key === "tags") void loadTags();
  if (key === "events") void loadEvents();
}

/* ---------------------------------------------------------------- загрузка */
async function loadAccounts() {
  try {
    const data = await getJson("/api/accounts");
    const rows = data.rows.map((row) => ({
      cls: row.status === "hold" ? "row-hold" : row.status === "dead" ? "row-dead" : "",
      cells: [
        escapeHtml(row.login) + (row.session ? "" : ' <span class="badge">нет сессии</span>'),
        `<span class="status-${escapeHtml(row.status)}">${escapeHtml(row.status_label)}</span>`,
        escapeHtml(row.proxy || "—"),
        String(row.posts),
        String(row.fails),
        escapeHtml(row.cooldown || "—"),
        escapeHtml(row.error || "—"),
        row.number
          ? `<span class="cell-actions"><button class="btn" data-login="${row.number}">Войти в VK</button></span>`
          : "—",
      ],
    }));
    renderTable(
      $("acc-table"),
      ["Логин", "Статус", "Прокси", "Постов", "Сбоев", "Отлёжка", "Последняя ошибка", ""],
      rows,
    );
  } catch (err) {
    toast(err.message, true);
  }
}

async function loadTags() {
  try {
    const data = await getJson("/api/tags");
    const rows = data.rows.map((row) => ({
      cls: row.status === "running" ? "row-run" : "",
      cells: [
        escapeHtml(row.query),
        escapeHtml(row.status_label || "—"),
        String(row.items),
        String(row.pages),
        String(row.open_jobs),
        escapeHtml(row.updated || "—"),
        `<span class="cell-actions"><button class="btn" data-reset="${escapeHtml(
          row.query,
        )}">В очередь</button></span>`,
      ],
    }));
    renderTable($("tag-table"), ["Тег", "Состояние", "Постов", "Страниц", "Окон", "Обновлён", ""], rows);
  } catch (err) {
    toast(err.message, true);
  }
}

async function loadEvents() {
  try {
    const data = await getJson("/api/events?limit=40");
    const rows = data.rows.map((row) => ({
      cls: row.level === "error" ? "row-dead" : row.level === "warn" ? "row-hold" : "",
      cells: [
        escapeHtml(row.time),
        escapeHtml(row.level_label || row.level),
        escapeHtml(row.kind),
        escapeHtml(row.account),
        escapeHtml(row.tag),
        escapeHtml(row.message),
      ],
    }));
    renderTable($("event-table"), ["Время", "Уровень", "Тип", "Аккаунт", "Тег", "Сообщение"], rows);
  } catch (err) {
    toast(err.message, true);
  }
}

/** Состояние шапки: карточки, пилюля в панели, доступность кнопок. */
function renderProgress(data) {
  state.running = data.running;
  $("kpi-posts").textContent = String(data.posts_total);
  $("kpi-posts-note").textContent = `+${data.posts_last_hour} за час · ссылок ${data.tag_links}`;
  $("kpi-tags").textContent = `${data.tags_finished} / ${data.tags_total}`;
  $("kpi-tags-note").textContent = `прогресс ${data.tags_pct}%`;
  $("kpi-bar").style.width = `${data.tags_pct}%`;
  $("kpi-accounts").textContent = String(data.accounts_total);
  $("kpi-accounts-note").textContent = `работает ${data.accounts_ready} · отлёжка ${data.accounts_hold} · мёртв ${data.accounts_dead}`;
  $("kpi-proxies").textContent = String(data.proxies_total);
  $("kpi-proxies-note").textContent = `аккаунтов в файле: ${data.accounts_file}`;
  $("run-stats").textContent =
    `постов в базе ${data.posts_total} · теги ${data.tags_finished}/${data.tags_total} (${data.tags_pct}%) · ` +
    `аккаунтов в файле ${data.accounts_file} · прокси ${data.proxies_total}`;

  const badge = $("rail-state");
  badge.classList.toggle("live", data.running);
  badge.innerHTML = (data.running ? '<span class="dot"></span>' : "") + escapeHtml(data.state);

  $("act-start").disabled = data.running;
  $("run-start").disabled = data.running;
  $("act-stop").disabled = !data.running;
  $("act-stop").classList.toggle("danger", data.running);
}

async function refreshProgress() {
  try {
    renderProgress(await getJson("/api/progress"));
  } catch (err) {
    toast(err.message, true);
  }
}

async function pollLogs() {
  try {
    const data = await getJson(`/api/logs?after=${state.logAfter}`);
    const box = $("log");
    if (data.lines.length) {
      const nearBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
      box.textContent = (box.textContent ? box.textContent + "\n" : "") + data.lines.join("\n");
      if (box.textContent.length > 200000) box.textContent = box.textContent.slice(-150000);
      if (nearBottom) box.scrollTop = box.scrollHeight;
    }
    state.logAfter = data.next;
    $("log-state").textContent = data.state;
  } catch (err) {
    /* панель могла закрыться — молчим, чтобы не спамить тостами */
  }
}

/* ---------------------------------------------------------------- действия */
async function runAction(path, body, fallback) {
  if (state.busy) return;
  state.busy = true;
  try {
    const data = await postJson(path, body);
    toast(data.message || fallback || "готово");
  } catch (err) {
    toast(err.message, true);
  } finally {
    state.busy = false;
    void refreshProgress();
  }
}

function startRun() {
  return runAction(
    "/api/run",
    {
      hours: Number($("run-period").value) || 0,
      passes: Number($("run-passes").value) || 1,
      workers: Number($("run-workers").value) || 1,
      validate_first: $("run-validate").checked,
      full: $("run-full").checked,
      dry_run: $("run-dry").checked,
    },
    "прогон запущен",
  );
}

async function refreshAll() {
  await refreshProgress();
  if (state.tab === "accounts") await loadAccounts();
  if (state.tab === "tags") await loadTags();
  if (state.tab === "events") await loadEvents();
  toast("обновлено");
}

async function addTags() {
  const content = $("tag-input").value.trim();
  if (!content) {
    toast("введите тег", true);
    return;
  }
  try {
    const data = await postJson("/api/tags/add", { content });
    $("tag-input").value = "";
    toast(data.message || "тег добавлен");
    await loadTags();
  } catch (err) {
    toast(err.message, true);
  }
}

async function copyLog() {
  try {
    await navigator.clipboard.writeText($("log").textContent);
    toast("лог скопирован");
  } catch (err) {
    toast(`браузер не дал скопировать: ${err.message}`, true);
  }
}

/* --------------------------------------------------------------------- модал */
function openModal(kind) {
  state.mode = kind;
  const mode = MODES[kind];
  $("modal-title").textContent = mode.title;
  $("modal-hint").textContent = mode.hint;
  $("modal-text").value = "";
  $("modal").classList.remove("hidden");
  if (kind !== "addAccounts") {
    void getJson(`/api/files/${kind}`)
      .then((data) => {
        $("modal-text").value = data.content;
      })
      .catch((err) => toast(err.message, true));
  }
  $("modal-text").focus();
}

function closeModal() {
  $("modal").classList.add("hidden");
  state.mode = null;
}

async function saveModal() {
  const mode = MODES[state.mode];
  if (!mode) return;
  try {
    const data = await mode.save($("modal-text").value);
    toast(data.message || "сохранено");
    closeModal();
    await mode.after();
    void refreshProgress();
  } catch (err) {
    toast(err.message, true);
  }
}

/* -------------------------------------------------------------- привязки UI */
function bind() {
  $("nav").addEventListener("click", (event) => {
    const button = event.target.closest(".nav-item");
    if (button) showTab(button.dataset.tab);
  });
  $("act-start").addEventListener("click", startRun);
  $("run-start").addEventListener("click", startRun);
  $("act-stop").addEventListener("click", () => runAction("/api/stop", {}, "процесс остановлен"));
  $("act-export").addEventListener("click", () => runAction("/api/export", {}, "выгрузка в Excel"));
  $("run-export").addEventListener("click", () => runAction("/api/export", {}, "выгрузка в Excel"));
  $("act-refresh").addEventListener("click", () => void refreshAll());
  $("run-open-exports").addEventListener("click", () =>
    runAction("/api/open", { target: "exports" }, "открываю папку экспорта"),
  );

  const validate = () =>
    runAction(
      "/api/validate",
      { workers: Number($("run-workers").value) || 1 },
      "проверка аккаунтов",
    );
  $("act-validate").addEventListener("click", validate);
  $("acc-validate").addEventListener("click", validate);

  $("acc-add").addEventListener("click", () => openModal("addAccounts"));
  $("acc-edit").addEventListener("click", () => openModal("accounts"));
  $("acc-open").addEventListener("click", () => runAction("/api/open", { target: "data" }, "открываю папку data"));
  $("acc-proxies").addEventListener("click", () => runAction("/api/proxies", {}, "проверка прокси"));
  $("acc-table").addEventListener("click", (event) => {
    const button = event.target.closest("[data-login]");
    if (!button) return;
    void runAction(
      "/api/login",
      { account: Number(button.dataset.login) },
      "вход в VK запущен — введите код в окне браузера",
    );
  });

  $("tag-edit").addEventListener("click", () => openModal("tags"));
  $("tag-open").addEventListener("click", () => runAction("/api/open", { target: "tags" }, "открываю tags.txt"));
  $("tag-add").addEventListener("click", () => void addTags());
  $("tag-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") void addTags();
  });
  $("tag-table").addEventListener("click", (event) => {
    const button = event.target.closest("[data-reset]");
    if (!button) return;
    const query = button.dataset.reset;
    if (!window.confirm(`Вернуть тег «${query}» в очередь? Собранные посты останутся в базе.`)) return;
    void runAction("/api/tags/reset", { query }, `тег «${query}» снова в очереди`);
  });

  $("log-clear").addEventListener("click", () => {
    $("log").textContent = "";
  });
  $("log-copy").addEventListener("click", () => void copyLog());

  $("modal-close").addEventListener("click", closeModal);
  $("modal-save").addEventListener("click", () => void saveModal());
  $("modal").addEventListener("click", (event) => {
    if (event.target === $("modal")) closeModal();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeModal();
  });
}

/* ------------------------------------------------------------------- старт */
async function init() {
  bind();
  try {
    const status = await getJson("/api/status");
    $("rail-path").textContent = status.data_dir;
    const settings = await getJson("/api/settings");
    $("run-workers").value = settings.max_accounts;
    $("run-passes").value = settings.repeat_passes;
    $("run-period").innerHTML = status.periods
      .map(
        (item, index) =>
          `<option value="${item.hours}"${index === 0 ? " selected" : ""}>${escapeHtml(item.label)}</option>`,
      )
      .join("");
  } catch (err) {
    toast(err.message, true);
  }
  await refreshProgress();
  void pollLogs();
  window.setInterval(() => void refreshProgress(), 1500);
  window.setInterval(() => void pollLogs(), 1000);
  window.setInterval(() => {
    if (state.tab === "accounts") void loadAccounts();
    if (state.tab === "events") void loadEvents();
  }, 3000);
}

document.addEventListener("DOMContentLoaded", () => void init());




