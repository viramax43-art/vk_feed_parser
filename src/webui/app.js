"use strict";

const accFilter = { mode: "all" };

/* Панель массового запуска: опрашивает /api/* и перерисовывает таблицы. */

const TABS = {
  run: {
    title: "Прогон",
    hint: "Период — это ЧТО собирать, а «Работать, ч» — сколько работать от «Старт»: поле подставляется по периоду («за 6 часов» → 6 ч, максимум 24 ч по умолчанию). Процесс идёт всё это время: сначала собирает период, дальше каждые «Пауза, с» доходит за новыми постами. «Работать, ч» = 0 — один проход: прогон заканчивается, как только период собран (уже собранные «за 6 часов» — это минута работы). «Стоп» — как Ctrl+C: прогресс сохраняется, следующий запуск продолжит с того же места.",
  },
  accounts: {
    title: "Аккаунты",
    hint: "Статусы пишет движок: работает / отлёжка / мёртв. «нет сессии» или «войти заново» в строке — нажмите «Войти в VK»: откроется браузер, телефон и пароль скрипт подставит сам, капчу решите там же. Когда таких аккаунтов несколько, нажмите «Войти во все, кому нужен вход» — она берёт всех без сессии и всех мёртвых (протух токен), окна пойдут по очереди. Прокси берётся из строки аккаунта или из proxies.txt.",
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
    hint: "Форматы: телефон;пароль;2FA;прокси или LOGIN:PASS:TOKEN. 2FA/прокси необязательны.",
    save: (text) => putJson("/api/files/accounts", { content: text }),
    after: () => loadAccounts(),
  },
  addProxies: {
    title: "Добавить прокси пачкой",
    hint: "По одному прокси в строке: host:port или user:pass@host:port, либо scheme://… Строки-заголовки и «телефон:пароль» пропускаются. Повтор не дублируется.",
    save: (text) => postJson("/api/proxies/add", { content: text }),
    after: () => loadAccounts(),
  },
  addAccounts: {
    title: "Добавить аккаунты",
    hint: "телефон;пароль;2FA;прокси или LOGIN:PASS:TOKEN. Повтор того же логина не дублируется — обновляет запись.",
    save: (text) => postJson("/api/accounts/add", { content: text }),
    after: () => loadAccounts(),
  },
};

const state = { tab: "run", logAfter: 0, running: false, mode: null, busy: false, loginBatch: {}, savedPause: 30 };

const $ = (id) => document.getElementById(id);

/* «Работать, ч» подставляется по периоду: «за 6 часов» → работать 6 ч от «Старт».
   Больше суток по умолчанию не подставляем («за 30 суток» не должен держать процесс
   месяц): сколько надо, вписывается руками. 0 — один проход, как раньше. */
const AUTO_DUTY_MAX_HOURS = 24;

const periodHours = () => Number($("run-period").value) || 0;

function syncDutyFromPeriod() {
  const hours = periodHours();
  $("run-duty").value = hours > 0 ? Math.min(hours, AUTO_DUTY_MAX_HOURS) : 0;
}

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
function selectedLogins() {
  return [...document.querySelectorAll("[data-check]:checked")].map((el) => el.dataset.check);
}

function syncAccFilterButtons() {
  document.querySelectorAll("[data-acc-filter]").forEach((btn) => {
    btn.classList.toggle("primary", btn.dataset.accFilter === accFilter.mode);
  });
}

async function deleteAccounts(body, question) {
  if (!window.confirm(question)) return;
  await runAction("/api/accounts/delete", body, "удаление выполнено");
  await loadAccounts();
}

/* Бейдж сессии в строке: нет файла — «нет сессии», файл есть, а токен мёртв —
   «войти заново» (иначе не видно, что мёртвому аккаунту помогает вход). */
function sessionBadge(row) {
  if (!row.needs_login) return "";
  return row.session
    ? ' <span class="badge" title="Файл сессии есть, но токен не работает — нужен вход заново">войти заново</span>'
    : ' <span class="badge">нет сессии</span>';
}

/* Кнопка «Войти во все, кому нужен вход»: одна кнопка — очередь входов по одному. */
function renderLoginAll(queue) {
  const button = $("acc-login-all");
  if (!button) return;
  if (queue.active) {
    const now = queue.login ? `: ${queue.login}` : "";
    button.textContent = `Остановить очередь (${queue.done}/${queue.total})${now}`;
    button.classList.add("danger");
    button.title = "Окна VK открываются по очереди — нажмите, чтобы закончить после текущего аккаунта";
  } else {
    button.textContent = "Войти во все, кому нужен вход";
    button.classList.remove("danger");
    button.title = "Открыть вход по очереди для всех без сессии и всех мёртвых (протух токен)";
  }
}

async function loginAll() {
  if (state.loginBatch.active) {
    await runAction("/api/login/all", { stop: true }, "останавливаю очередь входа");
    return;
  }
  try {
    const data = await getJson("/api/accounts");
    const pending = data.rows.filter((row) => row.number && row.needs_login);
    if (!pending.length) {
      toast("нет аккаунтов, кому нужен вход — у всех рабочая сессия");
      return;
    }
    const question =
      `Открыть вход в VK для ${pending.length} аккаунтов (нет сессии или протух токен)?\n` +
      "Окна будут открываться по очереди: в каждом введите пароль и решите капчу.\n" +
      "Очередь можно остановить этой же кнопкой или «Стоп».";
    if (!window.confirm(question)) return;
    const answer = await postJson("/api/login/all", {});
    toast(answer.message || "очередь входа запущена");
  } catch (err) {
    toast(err.message, true);
  }
}

async function loadAccounts() {
  try {
    const data = await getJson("/api/accounts");
    const visible = data.rows.filter((row) => {
      if (accFilter.mode === "dead") return row.status === "dead";
      if (accFilter.mode === "ready") return row.status === "ready";
      return true;
    });
    const rows = visible.map((row) => ({
      cls: row.status === "hold" ? "row-hold" : row.status === "dead" ? "row-dead" : "",
      cells: [
        `<input type="checkbox" class="acc-check" data-check="${escapeHtml(row.login)}">`,
        escapeHtml(row.login) + sessionBadge(row),
        `<span class="status-${escapeHtml(row.status)}">${escapeHtml(row.status_label)}</span>`,
        (row.needs_proxy ? '<span class="badge">нужен прокси</span>' : "") +
        escapeHtml(row.proxy || "—") +
        (row.shared_with && row.shared_with.length
          ? ` <span class="badge warn" title="делит адрес с: ${escapeHtml(row.shared_with.join(", "))}">общий IP</span>`
          : ""),
        String(row.posts),
        String(row.fails),
        escapeHtml(row.cooldown || "—"),
        escapeHtml(row.error || "—"),
        row.number
          ? `<span class="cell-actions"><button class="btn" data-login="${row.number}">Войти в VK</button> <button class="btn danger" data-delete="${escapeHtml(row.login)}">Удалить</button></span>`
          : "—",
      ],
    }));
    renderTable(
      $("acc-table"),
      ["", "Логин", "Статус", "Прокси", "Постов", "Сбоев", "Отлёжка", "Последняя ошибка", ""],
      rows,
    );
    syncAccFilterButtons();
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
        )}">В очередь</button> <button class="btn" data-zero="${escapeHtml(
          row.query,
        )}" title="Обнулить счётчики тега (Постов/Страниц/Проходов). Статус и курсор не меняются — это делает «В очередь»">Обнулить счётчики</button></span>`,
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
  state.loginBatch = data.login_batch || {};
  // «Обнулить счётчики» ставит отметку отсчёта (data.counter_reset_at): карточка
  // «Постов в базе» считает с неё, а «всего в базе» остаётся в подписи — сами посты
  // не удаляются, только цифры начинаются с нуля. Отметки нет — считаем всю базу.
  const sinceReset = Number(data.counter_reset_at || 0) > 0;
  $("kpi-posts-cap").textContent = sinceReset ? "Постов с обнуления" : "Постов в базе";
  $("kpi-posts").textContent = String(data.posts_since_reset);
  $("kpi-posts-note").textContent =
    `+${data.posts_last_hour} за час · ссылок ${data.tag_links_since_reset}` +
    (sinceReset ? ` · всего в базе ${data.posts_total}` : "");
  $("kpi-tags").textContent = `${data.tags_finished} / ${data.tags_total}`;
  $("kpi-tags-note").textContent = `прогресс ${data.tags_pct}%`;
  $("kpi-bar").style.width = `${data.tags_pct}%`;
  $("kpi-accounts").textContent = String(data.accounts_total);
  const noProxy = Number(data.accounts_needs_proxy || 0);
  $("kpi-accounts-note").textContent =
    `работает ${data.accounts_ready} · отлёжка ${data.accounts_hold} · мёртв ${data.accounts_dead}` +
    (noProxy ? ` · без прокси ${noProxy}` : "");
  $("kpi-proxies").textContent = String(data.proxies_total);
  $("kpi-proxies-note").textContent = `аккаунтов в файле: ${data.accounts_file}`;
  $("run-stats").textContent =
    `постов в базе ${data.posts_total} · теги ${data.tags_finished}/${data.tags_total} (${data.tags_pct}%) · ` +
    `аккаунтов в файле ${data.accounts_file} · прокси ${data.proxies_total}`;

  const badge = $("rail-state");
  badge.classList.toggle("live", data.running);
  badge.innerHTML = (data.running ? '<span class="dot"></span>' : "") + escapeHtml(data.state);

  const busy = data.running || Boolean(state.loginBatch.active);
  $("act-start").disabled = busy;
  $("run-start").disabled = busy;
  $("act-stop").disabled = !busy;
  $("act-stop").classList.toggle("danger", busy);
  renderLoginAll(state.loginBatch);
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
  const duty = Number($("run-duty").value) || 0;
  return runAction(
    "/api/run",
    {
      hours: Number($("run-period").value) || 0,
      // Время работы ограничено временем, а не числом проходов: иначе «Проходов»
      // обрезало бы её на первом же проходе.
      passes: duty > 0 ? 1 : Number($("run-passes").value) || 1,
      run_hours: duty,
      repeat_pause: Number($("run-pause").value) || 0,
      workers: Number($("run-workers").value) || 1,
      validate_first: $("run-validate").checked,
      full: $("run-full").checked,
      no_refresh: $("run-nofresh").checked,
      dry_run: $("run-dry").checked,
      // Окно эмулятора на виду: видно, как он обновляет ленту через F5.
      show_browser: $("run-showbrowser").checked,
      // Чем искать тег в запросе к VK: «с #» (хештег) или «без #» (слово). Выбор
      // стоит на странице «Теги» и действует на весь прогон.
      tag_query: $("tag-query").value,
    },
    duty > 0
      ? `работаю ${duty} ч — «Стоп» прерывает`
      : "прогон без «Работать, ч»: закончится, как только период собран",
  );
}

async function refreshAll() {
  await refreshProgress();
  if (state.tab === "accounts") await loadAccounts();
  if (state.tab === "tags") await loadTags();
  if (state.tab === "events") await loadEvents();
  toast("обновлено");
}

/**
 * «с #» / «без #» — настройка tag_query.
 *
 * У VK хештег (#тег) и слово без решётки — это РАЗНЫЕ выдачи, поэтому выбор стоит
 * рядом с тегами и применяется ко всему прогону: панель сохраняет значение, а
 * прогон получает его при старте (тег в запросе у всех один).
 *
 * Сохраняем именно PUT: data/settings.json правит маршрут PUT /api/settings (так же,
 * как правку файлов на странице). У runAction метод POST, а POST-маршрута у этого
 * адреса сервер не знает — панель отвечала «нет маршрута /api/settings», и режим
 * тега не сохранялся.
 */
async function saveTagQuery() {
  const value = $("tag-query").value;
  const mode = value === "word" || value === "auto" ? value : "hash";
  const label =
    mode === "word" ? "без # (слово)" : mode === "auto" ? "авто (по тегу)" : "с # (хештег)";
  try {
    const data = await putJson("/api/settings", { tag_query: mode });
    toast(data.message || `тег в запросе: ${label}`);
  } catch (err) {
    // Сказать вслух обязательно: иначе выбор в списке выглядит применённым, а прогон
    // возьмёт прежний режим из настроек.
    toast(err.message, true);
  }
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
  if (kind !== "addAccounts" && kind !== "addProxies") {
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
  // «Работать, ч» подставляется по периоду («за 6 часов» → работать 6 ч от «Старт»).
  $("run-period").addEventListener("change", syncDutyFromPeriod);
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
$("acc-proxies-add").addEventListener("click", () => openModal("addProxies"));
$("acc-proxies-bind").addEventListener("click", async () => {
  try {
    const data = await postJson("/api/proxies/bind", {});
    toast(data.message || "готово");
    await loadAccounts();
    void refreshProgress();
  } catch (err) {
    toast(err.message, true);
  }
});
  $("acc-edit").addEventListener("click", () => openModal("accounts"));
  $("acc-open").addEventListener("click", () => runAction("/api/open", { target: "data" }, "открываю папку data"));
  $("acc-proxies").addEventListener("click", () => runAction("/api/proxies", {}, "проверка прокси"));
  $("acc-login-all").addEventListener("click", () => void loginAll());
  document.querySelectorAll("[data-acc-filter]").forEach((btn) =>
    btn.addEventListener("click", () => {
      accFilter.mode = btn.dataset.accFilter;
      void loadAccounts();
    }),
  );
  $("acc-select-all").addEventListener("click", () => {
    document.querySelectorAll("[data-check]").forEach((el) => (el.checked = true));
  });
  $("acc-del-selected").addEventListener("click", () => {
    const logins = selectedLogins();
    if (!logins.length) {
      toast("ничего не выбрано", true);
      return;
    }
    void deleteAccounts({ logins }, `Удалить выбранные аккаунты (${logins.length}) из файла, базы и сессий?`);
  });
  $("acc-del-dead").addEventListener("click", () =>
    void deleteAccounts({ mode: "dead" }, "Удалить ВСЕ мёртвые аккаунты из файла, базы и сессий?"),
  );
  $("acc-table").addEventListener("click", (event) => {
    const del = event.target.closest("[data-delete]");
    if (del) {
      void deleteAccounts({ logins: [del.dataset.delete] }, `Удалить аккаунт ${del.dataset.delete}?`);
      return;
    }
    const button = event.target.closest("[data-login]");
    if (!button) return;
    void runAction(
      "/api/login",
      { account: Number(button.dataset.login) },
      "вход в VK запущен — введите код в окне браузера",
    );
  });

  $("tag-edit").addEventListener("click", () => openModal("tags"));
  // «с #» / «без #»: сохраняем сразу — это настройка прогона (tag_query).
  $("tag-query").addEventListener("change", () => void saveTagQuery());
  $("tag-open").addEventListener("click", () => runAction("/api/open", { target: "tags" }, "открываю tags.txt"));
  $("tag-add").addEventListener("click", () => void addTags());
  $("tag-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") void addTags();
  });
  $("tag-table").addEventListener("click", (event) => {
    // Две разные кнопки в строке тега: «В очередь» — состояние и курсор (собранные
    // посты остаются в базе), «Обнулить счётчики» — только накопленные
    // Постов/Страниц/Проходов (статус и курсор не трогаются).
    const zero = event.target.closest("[data-zero]");
    if (zero) {
      const query = zero.dataset.zero;
      const question =
        `Обнулить счётчики тега «${query}»?\n` +
        "«Постов», «Страниц» и «Проходов» станут нулями; статус и курсор тега не меняются.\n" +
        "Карточка «Постов в базе» тоже начнёт считать с нуля (посты из базы не удаляются).";
      if (!window.confirm(question)) return;
      void runAction("/api/tags/reset-stats", { query }, `счётчики тега «${query}» обнулены`);
      return;
    }
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
    // Период — что собирать, «Работать, ч» — сколько работать от «Старт» (0 — один
    // проход). Поле подставляется по периоду ниже, когда список периодов уже залит.
    state.savedPause = Number(settings.repeat_pause_sec) || 30;
    $("run-pause").value = state.savedPause;
    // Показ браузера — настройка show_browser: панель запоминает галочку и
    // передаёт её прогону (--show-browser): окно обхода ленты плюс окна эмулятора,
    // когда он освежает сессию через F5.
    $("run-showbrowser").checked = Boolean(settings.show_browser);
    // Чем искать тег в запросе к VK — настройка tag_query со страницы «Теги»:
    // «с #» (хештег, как было всегда), «без #» (слово) или «авто» (по каждому тегу).
    $("tag-query").value =
      settings.tag_query === "word" || settings.tag_query === "auto"
        ? settings.tag_query
        : "hash";
    $("run-duty").value = 0;
    $("run-period").innerHTML = status.periods
      .map(
        (item, index) =>
          `<option value="${item.hours}"${index === 0 ? " selected" : ""}>${escapeHtml(item.label)}</option>`,
      )
      .join("");
    syncDutyFromPeriod();
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




