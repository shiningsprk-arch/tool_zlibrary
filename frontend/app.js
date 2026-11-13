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
    jobs: {},
    jobByToken: {},
    pollTimer: null,
    lastError: null,
    doneTitle: '',
    doneTimer: null,
  };

  function jobIsActive(job) {
    return !!job && (job.status === 'running' || job.status === 'starting');
  }

  function activeJobForToken(token) {
    var jobId = state.jobByToken[token];
    var job = jobId ? state.jobs[jobId] : null;
    return jobIsActive(job) ? job : null;
  }

  function anyJobActive() {
    return Object.keys(state.jobs).some(function (jobId) {
      return jobIsActive(state.jobs[jobId]);
    });
  }

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

    var job = state.jobs[state.jobByToken[item.book_token]];
    if (jobIsActive(job)) {
      button.disabled = true;
    }
    if (job) {
      wrap.appendChild(job.node || createJobProgress(job));
      renderJob(job);
    }
    return wrap;
  }

  // ------------------------------------------------------------------ 下载

  function findResultRow(token) {
    if (!token) {
      return null;
    }
    return dom.resultList.querySelector(
      '.result-item[data-book-token="' + token + '"]');
  }

  function createJobProgress(job) {
    var node = document.createElement('div');
    node.className = 'row-progress';
    node.setAttribute('data-job-id', job.job_id);
    var barWrap = document.createElement('div');
    barWrap.className = 'mb-progress-linear mb-progress-linear--indeterminate';
    var bar = document.createElement('div');
    bar.className = 'mb-progress-linear__bar';
    barWrap.appendChild(bar);
    var text = document.createElement('p');
    text.className = 'hint';
    var cancel = document.createElement('button');
    cancel.className = 'mb-btn mb-btn--secondary small-btn';
    cancel.type = 'button';
    cancel.textContent = i18n.t('cancelBtn');
    cancel.addEventListener('click', function () {
      cancelJob(job.job_id);
    });
    node.appendChild(barWrap);
    node.appendChild(text);
    node.appendChild(cancel);
    job.node = node;
    job.parts = { barWrap: barWrap, bar: bar, text: text, cancel: cancel };
    return node;
  }

  function mountProgress(job, row) {
    row = row || findResultRow(job.book_token);
    if (!row) {
      return;
    }
    row.appendChild(job.node || createJobProgress(job));
    renderJob(job);
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

  function renderJob(job) {
    if (!job || !job.parts) {
      return;
    }
    var parts = job.parts;
    var active = jobIsActive(job);
    var percent = typeof job.progress === 'number' ? job.progress : 0;
    percent = Math.max(0, Math.min(100, Math.round(percent)));
    if (!active && (job.status === 'failed' || job.status === 'cancelled')) {
      parts.barWrap.classList.remove('mb-progress-linear--indeterminate');
      parts.bar.style.width = '100%';
      parts.text.className = 'hint job-error';
      parts.text.textContent = errorText({
        err: job.error || (job.status === 'cancelled' ? 'cancelled' : 'task.failed'),
        msg: job.detail || '',
      });
      parts.cancel.hidden = true;
      return;
    }
    if (!active && job.status === 'completed') {
      percent = 100;
    }
    var stageKey = {
      prepare: 'stagePrepare', downloading: 'stageDownloading',
      importing: 'stageImporting', done: 'stageDone',
    }[job.stage] || 'stagePrepare';
    var extra = '';
    if (job.stage === 'downloading' && job.downloaded) {
      extra = ' (' + fmtBytes(job.downloaded) +
        (job.total ? ' / ' + fmtBytes(job.total) : '') + ')';
    }
    parts.text.className = 'hint';
    parts.text.textContent = i18n.t('progressText', {
      percent: percent,
      stage: i18n.t(stageKey),
      extra: extra,
      account: job.account ? ' · ' + job.account : '',
    });
    parts.cancel.hidden = !active;
    parts.cancel.textContent = i18n.t('cancelBtn');
    if (active && (job.stage === 'prepare' || !percent)) {
      parts.barWrap.classList.add('mb-progress-linear--indeterminate');
      parts.bar.style.width = '';
    } else {
      parts.barWrap.classList.remove('mb-progress-linear--indeterminate');
      parts.bar.style.width = percent + '%';
    }
  }

  function enableRowButton(token) {
    var row = findResultRow(token);
    var button = row && row.querySelector('button[data-action="download"]');
    if (button) {
      button.disabled = false;
    }
  }

  function forgetTerminalJob(token) {
    var jobId = state.jobByToken[token];
    var job = jobId ? state.jobs[jobId] : null;
    if (!job || jobIsActive(job)) {
      return;
    }
    if (job.node && job.node.parentNode) {
      job.node.parentNode.removeChild(job.node);
    }
    delete state.jobs[jobId];
    delete state.jobByToken[token];
  }

  function handleTerminalJob(job, previousStatus) {
    if (job.notified) {
      return;
    }
    if (job.status !== 'completed' && job.status !== 'failed' &&
        job.status !== 'cancelled') {
      return;
    }
    if (previousStatus !== 'running' && previousStatus !== 'starting') {
      return;
    }
    job.notified = true;
    enableRowButton(job.book_token);
    if (job.status === 'completed') {
      renderError(null);
      state.doneTitle = job.current || '';
      updateDoneText();
      if (job.book_id) {
        dom.openBook.href = '/book/' + job.book_id;
        dom.openBook.hidden = false;
      } else {
        dom.openBook.hidden = true;
      }
      showDone();
      notify(i18n.t('doneMsg', { title: state.doneTitle }), 'success');
      loadAccounts();
      return;
    }
    var failure = {
      err: job.error || (job.status === 'cancelled' ? 'cancelled' : 'task.failed'),
      msg: job.detail || '',
    };
    renderError(failure);
    notify(errorText(failure), job.status === 'cancelled' ? 'warning' : 'error');
  }

  function updateJob(raw) {
    if (!raw || !raw.job_id) {
      return;
    }
    var job = state.jobs[raw.job_id];
    if (!job && !jobIsActive(raw)) {
      // 后端会短暂保留已结束任务供多页签/重连查询；本页没跟踪过的旧任务
      // 不再回放，也不要把已清理的旧进度节点重新挂回搜索结果。
      return;
    }
    var previousStatus = job ? job.status : null;
    if (!job) {
      job = { job_id: raw.job_id, book_token: raw.book_token, node: null };
      state.jobs[raw.job_id] = job;
    }
    Object.assign(job, raw);
    job.job_id = raw.job_id;
    job.book_token = raw.book_token || job.book_token;
    if (job.book_token) {
      state.jobByToken[job.book_token] = job.job_id;
    }
    var row = findResultRow(job.book_token);
    if (row) {
      mountProgress(job, row);
      var button = row.querySelector('button[data-action="download"]');
      if (button) {
        button.disabled = jobIsActive(job);
      }
    } else if (job.node) {
      renderJob(job);
    }
    handleTerminalJob(job, previousStatus);
  }

  function onDownload(token, row, button) {
    if (activeJobForToken(token)) {
      return;
    }
    renderError(null);
    state.doneTitle = '';
    hideDone();
    dom.openBook.hidden = true;
    forgetTerminalJob(token);
    if (button) {
      button.disabled = true;
    }
    postJson('download', { book_token: token }).then(function (rsp) {
      if (!rsp || rsp.err !== 'ok') {
        if (button) {
          button.disabled = false;
        }
        renderError(rsp || { err: 'request_failed' });
        return;
      }
      var titleEl = row && row.querySelector('.result-title');
      var jobId = (rsp.data && rsp.data.job_id) || token;
      var job = state.jobs[jobId];
      if (!job) {
        job = {
          job_id: jobId,
          book_token: token,
          status: 'starting',
          stage: 'prepare',
          progress: 0,
          current: titleEl ? titleEl.textContent : '',
        };
        state.jobs[jobId] = job;
      } else {
        // progress 轮询可能先于 download 响应看到这个任务并已挂好进度节点；
        // 这里只能合并，不能整对象替换，否则 node/parts 会丢。
        job.book_token = token;
        if (!job.current) {
          job.current = titleEl ? titleEl.textContent : '';
        }
      }
      state.jobByToken[token] = jobId;
      mountProgress(job, row);
      renderJob(job);
      ensurePolling();
    }).catch(function (err) {
      if (button) {
        button.disabled = false;
      }
      renderError({ err: 'request_failed', msg: String((err && err.message) || err) });
    });
  }

  function cancelJob(jobId) {
    postJson('cancel', { job_id: jobId }).then(function (rsp) {
      if (rsp && rsp.err !== 'ok') {
        renderError(rsp);
        return;
      }
      notify(i18n.t('cancelSent'), 'info');
    }).catch(function () { /* 下一次轮询会反映状态 */ });
  }

  function updateDoneText() {
    var title = state.doneTitle || '';
    dom.doneText.textContent = title ? i18n.t('doneMsg', { title: title }) : '';
    dom.openBook.textContent = title || i18n.t('openBook');
    dom.openBook.title = i18n.t('openBook');
  }

  function handleProgress(rsp, initial) {
    if (!rsp || !rsp.data) {
      return;
    }
    var jobs = rsp.data.jobs || [];
    jobs.forEach(function (raw) {
      if (initial && !jobIsActive(raw)) {
        return;
      }
      updateJob(raw);
    });
    if (anyJobActive()) {
      ensurePolling();
    } else {
      stopPolling();
    }
  }

  function pollOnce() {
    api('progress').then(function (rsp) {
      handleProgress(rsp, false);
    }).catch(function () { /* 瞬时错误不停表 */ });
  }

  function ensurePolling() {
    if (state.pollTimer) {
      return;
    }
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
      onDownload(item.getAttribute('data-book-token'), item, button);
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
    Object.keys(state.jobs).forEach(function (jobId) {
      renderJob(state.jobs[jobId]);
    });
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
    // 只恢复进行中的任务；completed/failed/cancelled 是上一次会话的残留，
    // 重新进入工具时不再回放 doneMsg / 旧错误。
    handleProgress(rsp, true);
  }).catch(function () { /* 宿主未登录等场景静默 */ });
})();
