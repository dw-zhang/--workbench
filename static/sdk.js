/* ==========================================================================
   独立版 SDK 适配层
   作用：把页面里的 window.__SMART_PAGE__.database 接到自建后端（/api/*），
        并在「设置」页注入钉钉连接配置卡。页面本身不需要任何改动。
   ========================================================================== */
(function () {
  'use strict';

  /* ---------- 1. 数据接口 ---------- */
  function qs(o) {
    return Object.keys(o).filter(function (k) { return o[k] !== undefined && o[k] !== null && o[k] !== ''; })
      .map(function (k) { return encodeURIComponent(k) + '=' + encodeURIComponent(o[k]); }).join('&');
  }
  function req(method, url, body) {
    var opt = { method: method, headers: { 'Content-Type': 'application/json' } };
    // 自带会话令牌：SDK 可能比登录层先执行，不能依赖外部 fetch 包装
    try {
      var t = localStorage.getItem('wb_token');
      if (t) opt.headers['Authorization'] = 'Bearer ' + t;
    } catch (e) { /* 隐私模式忽略 */ }
    if (body) opt.body = JSON.stringify(body);
    return fetch(url, opt).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error('HTTP ' + r.status + ' ' + t.slice(0, 120)); });
      return r.json();
    });
  }

  var api = {
    getSchema: function (o) {
      return req('GET', '/api/schema?db=' + encodeURIComponent((o || {}).databaseId || ''));
    },
    query: function (o) {
      o = o || {};
      var sorts = o.sorts || [];
      var s0 = sorts[0] || {};
      return req('GET', '/api/query?' + qs({
        db: o.databaseId, sort: s0.property, dir: s0.direction,
        size: o.pageSize, cursor: o.startCursor,
        filter: o.filter ? JSON.stringify(o.filter) : ''
      }));
    },
    aggregate: function (o) {
      o = o || {};
      return req('GET', '/api/aggregate?' + qs({
        db: o.databaseId,
        aggs: JSON.stringify(o.aggregates || [])
      }));
    },
    addRecord: function (o) {
      return req('POST', '/api/records', { db: o.databaseId, properties: o.properties })
        .then(function (r) { return { id: r.id }; });
    },
    updateRecord: function (o) {
      return req('PUT', '/api/records', { db: o.databaseId, recordId: o.recordId, properties: o.properties });
    },
    deleteRecord: function (o) {
      return req('DELETE', '/api/records', { db: o.databaseId, recordId: o.recordId });
    }
  };

  window.__SMART_PAGE__ = window.__SMART_PAGE__ || {};
  window.__SMART_PAGE__.database = api;
  window.WB_API_MODE = true;

  /* ---------- 2. 小工具 ---------- */
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  function toast(msg) {
    var t = document.getElementById('wb-toast');
    if (!t) { t = el('div'); t.id = 'wb-toast'; document.body.appendChild(t); }
    t.textContent = msg; t.className = 'on';
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.className = ''; }, 2800);
  }

  /* ---------- 3. 顶栏同步状态条 ---------- */
  function mountStatusBar() {
    if (document.getElementById('wb-syncbar')) return;
    var bar = el('div', 'wb-syncbar');
    bar.id = 'wb-syncbar';
    bar.innerHTML = '<span class="sb-dot"></span><span class="sb-text">正在检查同步状态…</span>' +
      '<button class="sb-btn" id="wb-sync-now">立即同步</button>' +
      '<button class="sb-btn ghost" id="wb-sync-cfg">连接设置</button>';
    document.body.appendChild(bar);
    bar.querySelector('#wb-sync-now').addEventListener('click', function () { doSync(this); });
    bar.querySelector('#wb-sync-cfg').addEventListener('click', function () { location.hash = '#/settings'; });
    refreshStatus();
    setInterval(refreshStatus, 30000);
    // 登录层可能晚一步写入令牌，1.5s 后补刷一次，避免状态条卡在 401
    setTimeout(refreshStatus, 1500);
  }

  function setBar(cls, text) {
    var bar = document.getElementById('wb-syncbar');
    if (!bar) return;
    bar.className = 'wb-syncbar ' + (cls || '');
    bar.querySelector('.sb-text').textContent = text;
  }

  var LAST = null;
  var _runTimer = null;
  function refreshStatus() {
    req('GET', '/api/status').then(function (s) {
      LAST = s;
      if (!s.configured) { setBar('warn', '未连接钉钉 · 点「连接设置」填写凭证'); }
      else if (s.running) {
        setBar('run', '同步中' + (s.syncProgress ? ' · ' + s.syncProgress : '…'));
        // 同步时每 8 秒刷一次进度（平时 30 秒一次就够）
        clearTimeout(_runTimer);
        _runTimer = setTimeout(refreshStatus, 8000);
      }
      else if (s.lastSync) { setBar('ok', '已连接' + (s.appName || s.name ? ' ' + (s.appName || s.name) : '') + ' · 最后同步 ' + s.lastSync.replace('T', ' ')); }
      else { setBar('warn', '已配置，但还没同步过 · 点「立即同步」'); }
      renderConnCard(s);
    }).catch(function (e) {
      setBar('err', '后端未连接：' + e.message);
    });
  }

  function doSync(btn) {
    if (btn) { btn.disabled = true; btn.textContent = '同步中…'; }
    req('POST', '/api/sync', {}).then(function (r) {
      toast(r.msg || '已开始同步');
      setTimeout(function () {
        refreshStatus();
        if (btn) { btn.disabled = false; btn.textContent = '立即同步'; }
        setTimeout(function () { if (window.__wbReloadData) window.__wbReloadData(); }, 3000);
      }, 1200);
    }).catch(function (e) {
      toast('同步失败：' + e.message);
      if (btn) { btn.disabled = false; btn.textContent = '立即同步'; }
    });
  }

  /* ---------- 4. 设置页：钉钉连接配置卡 ---------- */
  var FIELDS = [
    { k: 'appKey', label: 'AppKey', ph: '钉钉应用凭证 Client ID，如 dingxxxx', hint: '钉钉开放平台 → 应用开发 → 企业内部应用 → 凭证与基础信息' },
    { k: 'appSecret', label: 'AppSecret', ph: 'Client Secret（填写后不再回显）', hint: '同一个页面下方，只保存在你自己的服务器', type: 'password' },
    { k: 'corpId', label: '企业 CorpId', ph: '如 dingccxxxxxxxxxx', hint: '钉钉管理后台 → 首页 AgentId 区域的 CorpId' },
    { k: 'userId', label: '同步谁的数据（UserID）', ph: '如 1000000001', hint: '管理后台 → 通讯录 → 点成员 → 账号信息里的 userid' }
  ];

  function cardHTML(s) {
    var rows = FIELDS.map(function (f) {
      var v = f.k === 'appSecret' ? '' : (s && s[f.k]) || '';
      var ph = (f.k === 'appSecret' && s && s.hasSecret) ? '已保存（留空表示不修改）' : f.ph;
      return '<div class="conn-field">' +
        '<label>' + f.label + '</label>' +
        '<input type="' + (f.type || 'text') + '" data-k="' + f.k + '" value="' + String(v).replace(/"/g, '&quot;') + '" placeholder="' + ph + '">' +
        '<div class="conn-hint">' + f.hint + '</div></div>';
    }).join('');
    var logs = ((s && s.logs) || []).slice(0, 8).map(function (l) {
      return '<div class="conn-log ' + (l.ok ? 'ok' : 'err') + '"><span>' + (l.at || '').replace('T', ' ') +
        '</span><span class="cl-kind">' + (l.kind || '') + '</span><span>' + (l.msg || '') + '</span></div>';
    }).join('') || '<div class="conn-log"><span>暂无同步记录</span></div>';

    return '<div class="card-head"><div class="card-title">钉钉连接 <span class="tag ' +
      ((s && s.configured) ? 'st-已结束">已连接' : 'st-未开始">未配置') + '</span></div>' +
      '<button class="btn-mini primary" id="conn-save">保存配置</button></div>' +
      '<div class="conn-grid">' + rows + '</div>' +
      '<div class="conn-opts">' +
      '<label class="conn-inline">自动同步间隔 <input type="number" min="5" max="720" id="conn-interval" value="' +
      ((s && s.intervalMin) || 30) + '"> 分钟</label>' +
      '<label class="conn-inline"><input type="checkbox" id="conn-auto" ' +
      ((!s || s.autoSync) ? 'checked' : '') + '> 开启自动同步</label>' +
      '</div>' +
      '<div class="conn-actions">' +
      '<button class="btn-mini" id="conn-test">测试连接</button>' +
      '<button class="btn-mini" id="conn-sync">立即同步</button>' +
      '<span class="conn-msg" id="conn-msg"></span>' +
      '</div>' +
      '<div class="conn-logs-title">最近的同步记录</div>' +
      '<div class="conn-logs">' + logs + '</div>';
  }

  function renderConnCard(s) {
    var slot = document.getElementById('ding-conn-slot');
    if (!slot) return;
    if (slot.dataset.built !== '1') {
      var card = el('div', 'card');
      card.id = 'ding-conn-card';
      card.innerHTML = cardHTML(s);
      slot.appendChild(card);
      slot.dataset.built = '1';
      bindConn(card);
    } else if (s) {
      var tag = document.querySelector('#ding-conn-card .card-title .tag');
      if (tag) {
        tag.textContent = s.configured ? '已连接' : '未配置';
        tag.className = 'tag ' + (s.configured ? 'st-已结束' : 'st-未开始');
      }
      var logsBox = document.querySelector('#ding-conn-card .conn-logs');
      if (logsBox) {
        logsBox.innerHTML = (s.logs || []).slice(0, 8).map(function (l) {
          return '<div class="conn-log ' + (l.ok ? 'ok' : 'err') + '"><span>' + (l.at || '').replace('T', ' ') +
            '</span><span class="cl-kind">' + (l.kind || '') + '</span><span>' + (l.msg || '') + '</span></div>';
        }).join('') || '<div class="conn-log"><span>暂无同步记录</span></div>';
      }
    }
  }

  function bindConn(card) {
    var msg = card.querySelector('#conn-msg');
    function say(t, ok) { msg.textContent = t; msg.className = 'conn-msg ' + (ok ? 'ok' : 'err'); }

    card.querySelector('#conn-save').addEventListener('click', function () {
      var body = {};
      card.querySelectorAll('input[data-k]').forEach(function (i) { body[i.getAttribute('data-k')] = i.value.trim(); });
      body.intervalMin = parseInt(card.querySelector('#conn-interval').value, 10) || 30;
      body.autoSync = card.querySelector('#conn-auto').checked;
      say('保存中…', true);
      req('POST', '/api/config', body).then(function (r) {
        say('已保存，正在同步…', true);
        toast('配置已保存');
        card.querySelectorAll('input[data-k]').forEach(function (i) {
          if (i.getAttribute('data-k') === 'appSecret') i.value = '';
        });
        refreshStatus();
        req('POST', '/api/sync', {}).catch(function () {});
      }).catch(function (e) { say('保存失败：' + e.message, false); });
    });

    card.querySelector('#conn-test').addEventListener('click', function () {
      var body = {};
      card.querySelectorAll('input[data-k]').forEach(function (i) { body[i.getAttribute('data-k')] = i.value.trim(); });
      say('正在连接钉钉…', true);
      req('POST', '/api/test', body).then(function (r) {
        if (r.ok) say('连接成功：' + (r.user.name || r.user.userid) + '（unionId 已获取）', true);
        else say('连接失败：' + (r.error || '未知错误'), false);
      }).catch(function (e) { say('请求失败：' + e.message, false); });
    });

    card.querySelector('#conn-sync').addEventListener('click', function () {
      say('已触发同步，稍候刷新查看…', true);
      req('POST', '/api/sync', {}).then(function (r) {
        toast(r.msg || '已开始同步');
        setTimeout(refreshStatus, 4000);
      }).catch(function (e) { say('触发失败：' + e.message, false); });
    });
  }

  /* ---------- 5. 样式 ---------- */
  function injectCSS() {
    var css = '' +
      '.wb-syncbar{position:fixed;right:16px;bottom:16px;z-index:9998;display:flex;align-items:center;gap:10px;' +
      'background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:8px 12px;font-size:12px;color:#4b5563;' +
      'box-shadow:0 6px 20px rgba(15,23,42,.10)}' +
      '.wb-syncbar .sb-dot{width:8px;height:8px;border-radius:50%;background:#9ca3af;flex:none}' +
      '.wb-syncbar.ok .sb-dot{background:#16a34a}.wb-syncbar.warn .sb-dot{background:#f59e0b}' +
      '.wb-syncbar.err .sb-dot{background:#dc2626}.wb-syncbar.run .sb-dot{background:#2563eb;animation:pulse 1s infinite}' +
      '@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}' +
      '.wb-syncbar .sb-btn{border:1px solid #bfdbfe;background:#eff6ff;color:#2563eb;border-radius:6px;' +
      'padding:4px 9px;font-size:12px;cursor:pointer}' +
      '.wb-syncbar .sb-btn.ghost{background:#fff;border-color:#e5e7eb;color:#6b7280}' +
      '.conn-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px 16px;margin:12px 0 4px}' +
      '@media(max-width:760px){.conn-grid{grid-template-columns:1fr}}' +
      '.conn-field label{display:block;font-size:12px;color:#374151;font-weight:600;margin-bottom:4px}' +
      '.conn-field input{width:100%;box-sizing:border-box;border:1px solid #e5e7eb;border-radius:8px;' +
      'padding:8px 10px;font-size:13px;color:#111827;background:#fff}' +
      '.conn-field input:focus{outline:none;border-color:#2563eb;box-shadow:0 0 0 3px rgba(37,99,235,.08)}' +
      '.conn-hint{font-size:11px;color:#9ca3af;margin-top:3px;line-height:1.4}' +
      '.conn-opts{display:flex;gap:18px;align-items:center;margin:10px 0 4px;font-size:13px;color:#374151}' +
      '.conn-inline{display:inline-flex;align-items:center;gap:6px}' +
      '.conn-inline input[type=number]{width:70px;border:1px solid #e5e7eb;border-radius:6px;padding:4px 6px;font-size:13px}' +
      '.conn-actions{display:flex;align-items:center;gap:10px;margin:12px 0 6px}' +
      '.conn-msg{font-size:12px}.conn-msg.ok{color:#16a34a}.conn-msg.err{color:#dc2626}' +
      '.conn-logs-title{font-size:12px;color:#6b7280;margin:14px 0 6px}' +
      '.conn-logs{max-height:170px;overflow:auto;border:1px solid #f1f5f9;border-radius:8px;padding:6px 8px;background:#fcfcfd}' +
      '.conn-log{display:flex;gap:10px;font-size:12px;color:#6b7280;padding:3px 0;line-height:1.5}' +
      '.conn-log.ok .cl-kind{color:#16a34a}.conn-log.err .cl-kind{color:#dc2626}' +
      '.conn-log .cl-kind{min-width:46px;font-weight:600}';
    var st = document.createElement('style');
    st.textContent = css;
    document.head.appendChild(st);
  }

  /* ---------- 6. 启动 ---------- */
  function boot() {
    injectCSS();
    mountStatusBar();
    renderConnCard(LAST);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
