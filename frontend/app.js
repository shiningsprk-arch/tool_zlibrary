/**
 * zlibrary —— 工具前端逻辑（原生 JS，无构建）。
 *
 * 搜索/下载走后端；账号密码只在添加账号时提交一次，成功后立即清空 DOM，
 * 前端不保存任何密钥。
 */
(function () {
  'use strict';

  var bridge = window.MyBooksToolBridge || null;
  var i18n = window.MyBooksToolI18n.create();
  var toolId = (bridge && bridge.toolId) || 'zlibrary';
  var POLL_MS = 2000;

  function el(id) {
    return document.getElementById(id);
  }

  var dom = {
    healthBtn: el('health-btn'),
    healthStatus: el('health-status'),
    healthPanel: el('health-panel'),
    healthDetail: el('health-detail'),
    settingsBtn: el('settings-btn'),
    settingsPanel: el('settings-panel'),
    entryInput: el('entry-input'),
    dnsSelect: el('dns-select'),
    timeoutInput: el('timeout-input'),
    maxmbInput: el('maxmb-input'),
    tagInput: el('tag-input'),
    rotationSelect: el('rotation-select'),
    guestInput: el('guest-input'),
    saveBtn: el('save-btn'),
    qInput: el('q-input'),
    searchBtn: el('search-btn'),
    advBtn: el('adv-btn'),
    advPanel: el('adv-panel'),
    langSelect: el('lang-select'),
    orderSelect: el('order-select'),
    extChecks: el('ext-checks'),
    exactInput: el('exact-input'),
    yearfromInput: el('yearfrom-input'),
    yeartoInput: el('yearto-input'),
    notice: el('notice'),
    error: el('error'),
    progressArea: el('progress-area'),
    progress: el('progress'),
    progressText: el('progress-text'),
    cancelBtn: el('cancel-btn'),
    done: el('done'),
    doneText: el('done-text'),
    doneClose: el('done-close'),
    openBook: el('open-book'),
    results: el('results'),
    resultList: el('result-list'),
    sourceNote: el('source-note'),
    prevBtn: el('prev-btn'),
    nextBtn: el('next-btn'),
    pageInfo: el('page-info'),
    accountsToggle: el('accounts-toggle'),
    accountsPanel: el('accounts-panel'),
    accountSummary: el('account-summary'),
    accountList: el('account-list'),
    accountLabel: el('account-label'),
    accountMode: el('account-mode'),
    cookieFields: el('cookie-fields'),
    passwordFields: el('password-fields'),
    remixUserid: el('remix-userid'),
    remixUserkey: el('remix-userkey'),
    bsrvInput: el('bsrv-input'),
    accountEmail: el('account-email'),
    accountPassword: el('account-password'),
    addAccountBtn: el('add-account-btn'),
    accountNotice: el('account-notice'),
  };

  var state = {
    settings: null,
    meta: null,
    accounts: [],
    results: [],
    source: '',
    origin: '',
    q: '',
    page: 1,
    searching: false,
    running: false,
    pollTimer: null,
    lastProgress: null,
    lastError: null,
    doneTitle: '',
    doneTimer: null,
  };

  // ------------------------------------------------------------------ 基础

  function api(path, options) {
    if (bridge && typeof bridge.fetch === 'function') {
      return bridge.fetch(path, options).catch(function () {
        return rawApi(path, options);
      });
    }
    return rawApi(path, options);
  }

  function rawApi(path, options) {
    return fetch('/api/toolbox/tool/' + toolId + '/' + path, Object.assign({
      credentials: 'include',
    }, options || {})).then(function (resp) {
      return resp.json();
    });
  }

  function postJson(path, data) {
    return api(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data || {}),
    });
  }

  function applyTheme(theme) {
    document.body.setAttribute('data-theme', theme);
  }

  applyTheme((bridge && bridge.theme) || 'light');
  if (bridge && bridge.onThemeChange) {
    bridge.onThemeChange(applyTheme);
  }

  function notify(message, kind) {
    if (bridge && bridge.notify) {
      bridge.notify(message, kind || 'info');
    }
  }

  // ------------------------------------------------------------------ 设置

  function loadConfig() {
    return api('config').then(function (rsp) {
      if (!rsp || rsp.err !== 'ok' || !rsp.data) {
        renderError(rsp || { err: 'request_failed' });
        return;
      }
      state.settings = rsp.data;
      state.meta = rsp.data.filters || {};
      state.accounts = rsp.data.accounts || [];
      fillSettings(rsp.data);
      buildFilters(state.meta);
      renderAccounts();
    }).catch(function () { /* 宿主未登录/预览场景静默 */ });
  }

  function fillSettings(config) {
    dom.entryInput.value = config.entry_url || 'https://zlib.ch/';
    dom.dnsSelect.value = config.dns_mode || 'auto';
    dom.timeoutInput.value = config.timeout || 30;
    dom.maxmbInput.value = config.max_mb || 200;
    dom.tagInput.value = config.tag || '';
    dom.rotationSelect.value = config.rotation || 'least_used';
    dom.guestInput.checked = !!config.guest_enabled;
  }

  function saveSettings() {
    var body = {
      entry_url: dom.entryInput.value.trim() || 'https://zlib.ch/',
      dns_mode: dom.dnsSelect.value,
      timeout: parseInt(dom.timeoutInput.value, 10) || 30,
      max_mb: parseInt(dom.maxmbInput.value, 10) || 200,
      tag: dom.tagInput.value.trim(),
      rotation: dom.rotationSelect.value,
      guest_enabled: dom.guestInput.checked,
    };
    dom.saveBtn.disabled = true;
    postJson('config', body).then(function (rsp) {
      dom.saveBtn.disabled = false;
      if (!rsp || rsp.err !== 'ok') {
        renderError(rsp || { err: 'request_failed' });
        return;
      }
      state.settings = rsp.data;
      state.accounts = rsp.data.accounts || state.accounts;
      fillSettings(rsp.data);
      renderAccounts();
      renderError(null);
      notify(i18n.t('saved'), 'success');
    }).catch(function (err) {
      dom.saveBtn.disabled = false;
      renderError({ err: 'request_failed', msg: String((err && err.message) || err) });
    });
  }

  // ------------------------------------------------------------------ 健康检查

  function doHealth() {
    dom.healthBtn.disabled = true;
    dom.healthStatus.textContent = i18n.t('healthRunning');
    api('health').then(function (rsp) {
      dom.healthBtn.disabled = false;
      if (!rsp || rsp.err !== 'ok' || !rsp.data) {
        dom.healthStatus.textContent = '';
        renderError(rsp || { err: 'request_failed' });
        return;
      }
      var data = rsp.data;
      dom.healthStatus.textContent = i18n.t('healthOk', {
        origin: data.final_origin || '?',
        mode: data.eapi ? i18n.t('sourceEapi') : i18n.t('sourceHtml'),
      });
      var hops = (data.hops || []).map(function (hop, index) {
        return (index + 1) + '. ' + hop.status + ' ' + hop.url +
          (hop.challenge !== undefined ? ' [' + i18n.t('healthChallenge') + ']' : '');
      });
      dom.healthDetail.textContent = [
        i18n.t('healthEntry', { entry: data.entry || '' }),
        i18n.t('healthDns', { mode: data.dns_mode || '' }),
        i18n.t('healthLatency', { seconds: data.latency || 0 }),
        i18n.t('healthHops') + '\n' + hops.join('\n'),
      ].join('\n');
      dom.healthPanel.hidden = false;
      renderError(null);
    }).catch(function (err) {
      dom.healthBtn.disabled = false;
      dom.healthStatus.textContent = '';
      renderError({ err: 'request_failed', msg: String((err && err.message) || err) });
    });
  }

  // ------------------------------------------------------------------ 筛选

  function buildFilters(meta) {
    if (!meta) {
      return;
    }
    dom.langSelect.textContent = '';
    var any = document.createElement('option');
    any.value = '';
    any.textContent = i18n.t('anyLanguage');
    dom.langSelect.appendChild(any);
    (meta.languages || []).forEach(function (lang) {
      var option = document.createElement('option');
      option.value = lang.code;
      option.textContent = lang.label + ' [' + lang.code + ']';
      dom.langSelect.appendChild(option);
    });
    dom.orderSelect.textContent = '';
    (meta.orders || []).forEach(function (order) {
      var option = document.createElement('option');
      option.value = order.value;
      option.textContent = order.label;
      dom.orderSelect.appendChild(option);
    });
    dom.extChecks.textContent = '';
    (meta.extensions || []).forEach(function (ext) {
      var label = document.createElement('label');
      var box = document.createElement('input');
      box.type = 'checkbox';
      box.value = ext;
      label.appendChild(box);
      label.appendChild(document.createTextNode(ext.toUpperCase()));
      dom.extChecks.appendChild(label);
    });
  }

  function collectFilters() {
    return {
      languages: dom.langSelect.value ? [dom.langSelect.value] : [],
      extensions: Array.prototype.slice.call(dom.extChecks.querySelectorAll('input:checked'))
        .map(function (box) { return box.value; }),
      year_from: dom.yearfromInput.value ? parseInt(dom.yearfromInput.value, 10) : '',
      year_to: dom.yeartoInput.value ? parseInt(dom.yeartoInput.value, 10) : '',
      order: dom.orderSelect.value || '',
      exact: dom.exactInput.checked,
    };
  }

  // ------------------------------------------------------------------ 搜索

  function doSearch(page) {
    var q = dom.qInput.value.trim();
    if (!q || state.searching) {
      return;
    }
    if (state.running) {
      renderError({ err: 'task.running' });
      return;
    }
    state.doneTitle = '';
    hideDone();
    state.q = q;
    state.page = page || 1;
    state.searching = true;
    dom.notice.hidden = true;
    renderError(null);
    renderSearchButton();
    var body = Object.assign({ q: q, page: state.page }, collectFilters());
    postJson('search', body).then(function (rsp) {
      state.searching = false;
      renderSearchButton();
      if (!rsp || rsp.err !== 'ok' || !rsp.data) {
        renderError(rsp || { err: 'request_failed' });
        return;
      }
      state.results = rsp.data.books || [];
      state.source = rsp.data.source || '';
      state.origin = rsp.data.origin || '';
      state.page = rsp.data.page || state.page;
      renderResults(rsp.data);
    }).catch(function (err) {
      state.searching = false;
      renderSearchButton();
      renderError({ err: 'request_failed', msg: String((err && err.message) || err) });
    });
  }

  function renderSearchButton() {
    dom.searchBtn.disabled = state.searching;
    dom.searchBtn.textContent = i18n.t(state.searching ? 'searching' : 'searchBtn');
  }

  function renderResults(data) {
    dom.resultList.textContent = '';
    state.results.forEach(function (item) {
      dom.resultList.appendChild(renderResultItem(item));
    });
    dom.results.hidden = false;
    if (!state.results.length) {
      dom.notice.textContent = i18n.t('noResults');
      dom.notice.hidden = false;
    }
    dom.sourceNote.textContent = state.source
      ? i18n.t('sourceNote', {
        source: state.source === 'eapi' ? i18n.t('sourceEapi') : i18n.t('sourceHtml'),
        origin: state.origin,
      }) : '';
    dom.pageInfo.textContent = i18n.t('pageInfo', { page: state.page });
    dom.prevBtn.disabled = state.page <= 1;
    var totalPages = data && data.total_pages ? data.total_pages : 0;
    dom.nextBtn.disabled = totalPages ? state.page >= totalPages : !state.results.length;
  }

  function renderResultItem(item) {
    var wrap = document.createElement('div');
    wrap.className = 'result-item';
    wrap.setAttribute('data-book-token', item.book_token);

    if (item.cover) {
      var img = document.createElement('img');
      img.className = 'result-cover';
      img.src = item.cover;
      img.alt = '';
      img.loading = 'lazy';
      img.referrerPolicy = 'no-referrer';
      wrap.appendChild(img);
    }

    var chip = document.createElement('span');
    chip.className = 'ext-chip';
    chip.textContent = (item.extension || '?').toUpperCase();
    wrap.appendChild(chip);

    var text = document.createElement('div');
    text.className = 'result-text';
    var title = document.createElement('div');
    title.className = 'result-title';
    title.textContent = item.title || i18n.t('untitled');
    text.appendChild(title);
    var sub = document.createElement('div');
    sub.className = 'result-sub';
    sub.textContent = [item.author, item.year, item.publisher, item.language, item.size]
      .filter(Boolean).join(' · ');
    text.appendChild(sub);
    wrap.appendChild(text);

    var actions = document.createElement('div');
    actions.className = 'result-actions';
    var button = document.createElement('button');
    button.className = 'mb-btn';
    button.type = 'button';
    button.setAttribute('data-action', 'download');
    button.textContent = i18n.t('downloadBtn');
    actions.appendChild(button);
    wrap.appendChild(actions);
    return wrap;
  }

  // ------------------------------------------------------------------ 下载

  function mountProgress(bookToken, row) {
    row = row || dom.resultList.querySelector(
      '.result-item[data-book-token="' + bookToken + '"]');
    if (!row) {
      return;
    }
    var holder = row.querySelector('.row-progress');
    if (!holder) {
      holder = document.createElement('div');
      holder.className = 'row-progress';
      row.appendChild(holder);
    }
    holder.appendChild(dom.progressArea);
    dom.progressArea.hidden = false;
  }

  function hideDone() {
    dom.done.hidden = true;
    if (state.doneTimer) {
      clearTimeout(state.doneTimer);
      state.doneTimer = null;
    }
  }

  function showDone() {
    dom.done.hidden = false;
    if (state.doneTimer) {
      clearTimeout(state.doneTimer);
    }
    state.doneTimer = setTimeout(hideDone, 8000);
  }

  function onDownload(token, row) {
    if (state.running) {
      renderError({ err: 'task.running' });
      return;
    }
    renderError(null);
    state.doneTitle = '';
    hideDone();
    dom.openBook.hidden = true;
    postJson('download', { book_token: token }).then(function (rsp) {
      if (!rsp || rsp.err !== 'ok') {
        renderError(rsp || { err: 'request_failed' });
        return;
      }
      mountProgress(token, row);
      setRunning(true);
      renderProgress({ progress: 0, stage: 'prepare' });
      startPolling();
    }).catch(function (err) {
      renderError({ err: 'request_failed', msg: String((err && err.message) || err) });
    });
  }

  function fmtBytes(n) {
    if (typeof n !== 'number' || n <= 0) {
      return '';
    }
    if (n >= 1024 * 1024 * 1024) {
      return (n / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
    }
    if (n >= 1024 * 1024) {
      return (n / (1024 * 1024)).toFixed(1) + ' MB';
    }
    return Math.round(n / 1024) + ' KB';
  }

  function renderProgress(data) {
    var percent = typeof data.progress === 'number' ? data.progress : 0;
    var stageKey = {
      prepare: 'stagePrepare', downloading: 'stageDownloading',
      importing: 'stageImporting', done: 'stageDone',
    }[data.stage] || 'stagePrepare';
    var extra = '';
    if (data.stage === 'downloading' && data.downloaded) {
      extra = ' (' + fmtBytes(data.downloaded) +
        (data.total ? ' / ' + fmtBytes(data.total) : '') + ')';
    }
    dom.progressText.textContent = i18n.t('progressText', {
      percent: percent,
      stage: i18n.t(stageKey),
      extra: extra,
      account: data.account ? ' · ' + data.account : '',
    });
  }

  function setRunning(running) {
    state.running = running;
    dom.progressArea.hidden = !running;
    dom.cancelBtn.hidden = !running;
  }

  function updateDoneText() {
    var title = state.doneTitle || '';
    dom.doneText.textContent = title ? i18n.t('doneMsg', { title: title }) : '';
    dom.openBook.textContent = title || i18n.t('openBook');
    dom.openBook.title = i18n.t('openBook');
  }

  function handleProgress(rsp) {
    if (!rsp || !rsp.data) {
      return;
    }
    var data = rsp.data;
    var status = data.status || 'idle';
    if (status !== 'running' && status !== 'starting') {
      stopPolling();
      setRunning(false);
      if (status === 'completed') {
        renderError(null);
        state.doneTitle = data.current || '';
        updateDoneText();
        if (data.book_id) {
          dom.openBook.href = '/book/' + data.book_id;
          dom.openBook.hidden = false;
        } else {
          dom.openBook.hidden = true;
        }
        showDone();
        notify(i18n.t('doneMsg', { title: state.doneTitle }), 'success');
        loadAccounts();
      } else {
        renderError(rsp);
      }
      return;
    }
    state.lastProgress = data;
    renderProgress(data);
  }

  function pollOnce() {
    api('progress').then(handleProgress).catch(function () { /* 瞬时错误不停表 */ });
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(pollOnce, POLL_MS);
    pollOnce();
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  // ------------------------------------------------------------------ 账号

  function loadAccounts() {
    return api('accounts').then(function (rsp) {
      if (!rsp || rsp.err !== 'ok' || !rsp.data) {
        return;
      }
      state.accounts = rsp.data.accounts || [];
      renderAccounts();
    }).catch(function () { /* 静默 */ });
  }

  function accountStatusText(account) {
    var key = 'accountStatus' + account.status.charAt(0).toUpperCase() +
      account.status.slice(1);
    var text = i18n.t(key);
    return text === key ? account.status : text;
  }

  function quotaText(account) {
    if (account.quota_today === null || account.quota_limit === null ||
        account.quota_today === undefined || account.quota_limit === undefined) {
      return i18n.t('quotaUnknown');
    }
    return i18n.t('quotaText', {
      today: account.quota_today,
      limit: account.quota_limit,
      remaining: account.remaining,
    });
  }

  function renderAccounts() {
    dom.accountList.textContent = '';
    if (!state.accounts.length) {
      var empty = document.createElement('p');
      empty.className = 'hint';
      empty.textContent = i18n.t('noAccounts');
      dom.accountList.appendChild(empty);
    } else {
      var table = document.createElement('table');
      table.className = 'account-table';
      var head = document.createElement('tr');
      ['accountLabel', 'statusLabel', 'quotaLabel', 'lastUsedLabel', 'actionsLabel']
        .forEach(function (key) {
          var th = document.createElement('th');
          th.textContent = i18n.t(key);
          head.appendChild(th);
        });
      table.appendChild(head);
      state.accounts.forEach(function (account) {
        var row = document.createElement('tr');
        row.setAttribute('data-account-id', account.id);
        var name = document.createElement('td');
        var strong = document.createElement('div');
        strong.textContent = account.label || account.id;
        name.appendChild(strong);
        if (account.email) {
          var email = document.createElement('div');
          email.className = 'hint';
          email.textContent = account.email;
          name.appendChild(email);
        }
        row.appendChild(name);
        var status = document.createElement('td');
        var badge = document.createElement('span');
        badge.className = 'status-badge status-' + account.status;
        badge.textContent = accountStatusText(account);
        status.appendChild(badge);
        if (account.last_error) {
          var errorTextEl = document.createElement('div');
          errorTextEl.className = 'hint';
          errorTextEl.textContent = account.last_error;
          status.appendChild(errorTextEl);
        }
        row.appendChild(status);
        var quota = document.createElement('td');
        quota.textContent = quotaText(account);
        row.appendChild(quota);
        var last = document.createElement('td');
        last.textContent = account.last_used ? formatTime(account.last_used) : '—';
        row.appendChild(last);
        var actions = document.createElement('td');
        actions.appendChild(accountButton('test', account.id, 'testBtn'));
        actions.appendChild(accountButton('toggle', account.id,
          account.enabled ? 'disableBtn' : 'enableBtn'));
        actions.appendChild(accountButton('delete', account.id, 'deleteBtn'));
        row.appendChild(actions);
        table.appendChild(row);
      });
      dom.accountList.appendChild(table);
    }
    var usable = state.accounts.filter(function (account) {
      return account.enabled && account.status !== 'invalid' && account.status !== 'exhausted';
    }).length;
    dom.accountSummary.textContent = i18n.t('accountSummary', {
      total: state.accounts.length, usable: usable,
    });
  }

  function accountButton(action, id, labelKey) {
    var button = document.createElement('button');
    button.className = 'mb-btn mb-btn--secondary small-btn';
    button.type = 'button';
    button.setAttribute('data-account-action', action);
    button.setAttribute('data-account-id', id);
    button.textContent = i18n.t(labelKey);
    return button;
  }

  function formatTime(timestamp) {
    try {
      return new Date(timestamp * 1000).toLocaleString();
    } catch (err) {
      return String(timestamp);
    }
  }

  function onAccountAction(action, id) {
    var body = { id: id };
    if (action === 'test') {
      body.action = 'test';
    } else if (action === 'toggle') {
      var account = state.accounts.filter(function (item) { return item.id === id; })[0];
      body.action = 'update';
      body.enabled = !(account && account.enabled);
    } else if (action === 'delete') {
      if (!window.confirm(i18n.t('confirmDelete'))) {
        return;
      }
      body.action = 'delete';
    }
    postJson('accounts', body).then(function (rsp) {
      if (!rsp || rsp.err !== 'ok') {
        dom.accountNotice.textContent = errorText(rsp || { err: 'request_failed' });
        return;
      }
      state.accounts = (rsp.data && rsp.data.accounts) || state.accounts;
      renderAccounts();
      dom.accountNotice.textContent = action === 'test'
        ? i18n.t('accountTested') : i18n.t('accountSaved');
    }).catch(function (err) {
      dom.accountNotice.textContent = errorText({
        err: 'request_failed', msg: String((err && err.message) || err),
      });
    });
  }

  function clearAccountInputs() {
    dom.accountLabel.value = '';
    dom.remixUserid.value = '';
    dom.remixUserkey.value = '';
    dom.bsrvInput.value = '';
    dom.accountEmail.value = '';
    dom.accountPassword.value = '';
  }

  function addAccount() {
    var mode = dom.accountMode.value;
    var body = {
      action: 'add',
      mode: mode,
      label: dom.accountLabel.value.trim(),
    };
    if (mode === 'password') {
      body.email = dom.accountEmail.value.trim();
      body.password = dom.accountPassword.value;
      if (!body.email || !body.password) {
        dom.accountNotice.textContent = i18n.t('needCredentials');
        return;
      }
    } else {
      body.remix_userid = dom.remixUserid.value.trim();
      body.remix_userkey = dom.remixUserkey.value.trim();
      body.bsrv = dom.bsrvInput.value.trim();
      if (!body.remix_userid && !body.remix_userkey && !body.bsrv) {
        dom.accountNotice.textContent = i18n.t('needCookies');
        return;
      }
    }
    dom.addAccountBtn.disabled = true;
    dom.accountNotice.textContent = i18n.t('accountAdding');
    postJson('accounts', body).then(function (rsp) {
      dom.addAccountBtn.disabled = false;
      clearAccountInputs();
      if (!rsp || rsp.err !== 'ok') {
        dom.accountNotice.textContent = errorText(rsp || { err: 'request_failed' });
        return;
      }
      state.accounts = (rsp.data && rsp.data.accounts) || state.accounts;
      renderAccounts();
      dom.accountNotice.textContent = i18n.t('accountAdded');
    }).catch(function (err) {
      dom.addAccountBtn.disabled = false;
      clearAccountInputs();
      dom.accountNotice.textContent = errorText({
        err: 'request_failed', msg: String((err && err.message) || err),
      });
    });
  }

  // ------------------------------------------------------------------ 错误

  function errorText(rsp) {
    var code = (rsp && rsp.err) || 'unknown';
    var key = 'error.' + code;
    var text = i18n.t(key);
    var msg = (rsp && rsp.msg) || '';
    if (text === key) {
      return msg || code;
    }
    return msg ? text + ' — ' + msg : text;
  }

  function renderError(rsp) {
    state.lastError = rsp;
    dom.error.textContent = rsp ? errorText(rsp) : '';
    dom.error.hidden = !rsp;
  }

  // ------------------------------------------------------------------ 事件

  dom.healthBtn.addEventListener('click', doHealth);
  dom.settingsBtn.addEventListener('click', function () {
    dom.settingsPanel.hidden = !dom.settingsPanel.hidden;
  });
  dom.saveBtn.addEventListener('click', saveSettings);
  dom.searchBtn.addEventListener('click', function () { doSearch(1); });
  dom.qInput.addEventListener('keydown', function (event) {
    if (event.key === 'Enter') {
      doSearch(1);
    }
  });
  dom.advBtn.addEventListener('click', function () {
    dom.advPanel.hidden = !dom.advPanel.hidden;
  });
  dom.prevBtn.addEventListener('click', function () {
    if (state.page > 1) {
      doSearch(state.page - 1);
    }
  });
  dom.nextBtn.addEventListener('click', function () {
    doSearch(state.page + 1);
  });
  dom.cancelBtn.addEventListener('click', function () {
    postJson('cancel', {}).then(function (rsp) {
      if (rsp && rsp.err !== 'ok') {
        renderError(rsp);
        return;
      }
      notify(i18n.t('cancelSent'), 'info');
    }).catch(function () { /* 下一次轮询会反映状态 */ });
  });
  dom.doneClose.addEventListener('click', hideDone);
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
      hideDone();
    }
  });
  window.addEventListener('pageshow', hideDone);
  dom.accountsToggle.addEventListener('click', function () {
    dom.accountsPanel.hidden = !dom.accountsPanel.hidden;
  });
  function syncAccountMode() {
    var password = dom.accountMode.value === 'password';
    dom.passwordFields.hidden = !password;
    dom.cookieFields.hidden = password;
  }

  dom.accountMode.addEventListener('change', syncAccountMode);
  syncAccountMode();
  dom.addAccountBtn.addEventListener('click', addAccount);

  dom.resultList.addEventListener('click', function (event) {
    var button = event.target.closest('button[data-action="download"]');
    if (!button) {
      return;
    }
    var item = button.closest('.result-item');
    if (item) {
      onDownload(item.getAttribute('data-book-token'), item);
    }
  });

  dom.accountList.addEventListener('click', function (event) {
    var button = event.target.closest('button[data-account-action]');
    if (!button) {
      return;
    }
    onAccountAction(button.getAttribute('data-account-action'),
      button.getAttribute('data-account-id'));
  });

  i18n.onChange(function () {
    renderSearchButton();
    buildFilters(state.meta);
    renderAccounts();
    updateDoneText();
    if (state.results.length) {
      renderResults({ page: state.page });
    }
    if (state.running && state.lastProgress) {
      renderProgress(state.lastProgress);
    }
    if (state.lastError) {
      renderError(state.lastError);
    }
  });

  i18n.ready.then(function () {
    renderSearchButton();
    updateDoneText();
  });
  loadConfig();
  loadAccounts();
  api('progress').then(function (rsp) {
    if (!rsp || !rsp.data) {
      return;
    }
    var status = rsp.data.status || 'idle';
    // 只恢复进行中的任务；completed/failed/cancelled 是上一次会话的残留，
    // 重新进入工具时不再回放 doneMsg / 旧错误。
    if (status === 'running' || status === 'starting') {
      setRunning(true);
      renderProgress(rsp.data);
      startPolling();
    }
  }).catch(function () { /* 宿主未登录等场景静默 */ });
})();
