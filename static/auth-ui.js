/* ==========================================================================
   身份层：登录 / 角色 / 数据隔离的前端配合
   - 管理员：密码登录，看到「人员管理 / 平台配置」
   - 员工：钉钉免登，只看到自己的数据（服务端已按 person_id 过滤）
   - 未登录：全屏登录卡，页面其余部分不渲染数据
   ========================================================================== */
(function () {
  'use strict';

  var TOKEN_KEY = 'wb_token';
  var ME = null;
  var CORP = '';

  function token() {
    try { return localStorage.getItem(TOKEN_KEY) || ''; } catch (e) { return ''; }
  }
  function setToken(t) {
    try { if (t) localStorage.setItem(TOKEN_KEY, t); else localStorage.removeItem(TOKEN_KEY); }
    catch (e) { /* 隐私模式/管控浏览器：会话不落盘，仅本次有效 */ }
  }

  /* 支持 ?token=xxx 直接进入（内部调试 / 单点跳转用）
     读完立刻从地址栏抹掉，避免 token 留在浏览器历史里 */
  (function () {
    try {
      var m = /[?&]token=([^&#]+)/.exec(location.search);
      if (!m) return;
      setToken(decodeURIComponent(m[1]));
      var rest = location.search.replace(/([?&])token=[^&#]*&?/, '$1').replace(/[?&]$/, '');
      var url = location.pathname + (rest && rest !== '?' ? rest : '') + location.hash;
      history.replaceState(null, '', url);
    } catch (e) {}
  })();

  /* ---------- fetch 包装：自动带 token ---------- */
  var _fetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init || {};
    var h = new Headers(init.headers || {});
    var t = token();
    if (t) h.set('Authorization', 'Bearer ' + t);
    init.headers = h;
    return _fetch(input, init);
  };
  window.WB_API = function (method, url, body) {
    var opt = { method: method, headers: { 'Content-Type': 'application/json' } };
    if (body) opt.body = JSON.stringify(body);
    return window.fetch(url, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) { var e = new Error(d.error || ('HTTP ' + r.status)); e.code = r.status; throw e; }
        return d;
      });
    });
  };

  /* ---------- 样式 ---------- */
  function css() {
    var s = document.createElement('style');
    s.textContent = '' +
      '.wb-login{position:fixed;inset:0;z-index:10000;background:linear-gradient(135deg,#eff6ff,#f8fafc 60%,#eef2ff);' +
      'display:flex;align-items:center;justify-content:center;font-family:inherit}' +
      '.wb-login-card{width:380px;max-width:92vw;background:#fff;border:1px solid #e5e7eb;border-radius:16px;' +
      'padding:26px 26px 22px;box-shadow:0 18px 50px rgba(15,23,42,.12)}' +
      '.wb-login-card h2{margin:0 0 4px;font-size:19px;color:#111827}' +
      '.wb-login-card .sub{font-size:12.5px;color:#6b7280;margin-bottom:18px;line-height:1.6}' +
      '.wb-login-card label{display:block;font-size:12px;font-weight:600;color:#374151;margin:12px 0 5px}' +
      '.wb-login-card input{width:100%;box-sizing:border-box;padding:9px 11px;border:1px solid #e5e7eb;' +
      'border-radius:8px;font-size:13px;color:#111827}' +
      '.wb-login-card button{width:100%;margin-top:14px;padding:10px;border:none;border-radius:9px;' +
      'background:#2563eb;color:#fff;font-size:13.5px;cursor:pointer}' +
      '.wb-login-card button:disabled{background:#93c5fd;cursor:default}' +
      '.wb-login-card .ghost{background:#fff;border:1px solid #dbe1ea;color:#374151;margin-top:8px}' +
      '.wb-login-msg{font-size:12px;margin-top:10px;min-height:16px;color:#dc2626}' +
      '.wb-login-msg.ok{color:#16a34a}' +
      '.wb-login-tabs{display:flex;gap:6px;margin-bottom:6px}' +
      '.wb-login-tabs span{flex:1;text-align:center;font-size:12.5px;padding:7px 0;border-radius:8px;' +
      'background:#f3f4f6;color:#6b7280;cursor:pointer}' +
      '.wb-login-tabs span.on{background:#e0edff;color:#1d4ed8;font-weight:600}' +
      '.wb-ident{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:#374151;' +
      'background:#f3f4f6;border-radius:999px;padding:4px 10px;cursor:pointer;margin-right:10px}' +
      '.wb-ident:hover{background:#e5e7eb}' +
      '.wba-topbtn{border:1px solid #e5e7eb;background:#fff;border-radius:999px;padding:4px 12px;' +
      'font-size:12px;color:#6b7280;cursor:pointer;margin-right:10px}' +
      '.wba-topbtn:hover{border-color:#fca5a5;color:#dc2626;background:#fef2f2}' +
      '.wba-danger{border-color:#fecaca !important;background:#fef2f2 !important;color:#dc2626 !important}' +
      '.wba-danger:hover{background:#fee2e2 !important}' +
      '.wba-mask{position:fixed;inset:0;z-index:10050;background:rgba(15,23,42,.45);display:flex;' +
      'align-items:center;justify-content:center;padding:20px;box-sizing:border-box}' +
      '.wba-card{background:#fff;border-radius:14px;width:min(900px,94vw);max-height:88vh;' +
      'overflow:auto;padding:20px 22px;box-shadow:0 20px 60px rgba(15,23,42,.28);box-sizing:border-box}' +
      '.wba-card h3{margin:0 0 4px;font-size:16px;color:#111827}' +
      '.wba-card .m-sub{font-size:12px;color:#6b7280;margin-bottom:12px}' +
      '.wb-tools{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 14px}' +
      '.wb-tools button{border:1px solid #dbe1ea;background:#fff;border-radius:8px;padding:6px 11px;' +
      'font-size:12px;color:#374151;cursor:pointer}' +
      '.wb-tools button:hover{border-color:#2563eb;color:#2563eb}' +
      '.wb-table{width:100%;border-collapse:collapse;font-size:12.5px}' +
      '.wb-table th{text-align:left;color:#6b7280;font-weight:600;padding:7px 8px;border-bottom:1px solid #eef1f5}' +
      '.wb-table td{padding:8px;border-bottom:1px solid #f3f5f9;color:#111827;vertical-align:middle}' +
      '.wb-badge{display:inline-block;font-size:11px;padding:2px 7px;border-radius:999px;margin-right:4px}' +
      '.wb-badge.ok{background:#dcfce7;color:#15803d}.wb-badge.warn{background:#fef3c7;color:#b45309}' +
      '.wb-badge.err{background:#fee2e2;color:#b91c1c}.wb-badge.mute{background:#f1f5f9;color:#64748b}' +
      '.wb-close{float:right;border:none;background:#f3f4f6;border-radius:8px;padding:5px 10px;' +
      'font-size:12px;color:#4b5563;cursor:pointer}' +
      '.wb-plat{margin:14px 0;border-top:1px solid #f1f5f9;padding-top:12px}' +
      '.wb-plat h4{margin:0 0 8px;font-size:13px;color:#111827}' +
      '.wb-plat .row{display:grid;grid-template-columns:170px 1fr;gap:8px;align-items:center;margin-bottom:7px}' +
      '.wb-plat .row span{font-size:12px;color:#4b5563}' +
      '.wb-plat input{width:100%;box-sizing:border-box;padding:7px 9px;border:1px solid #e5e7eb;border-radius:7px;font-size:12.5px}' +
      '.wb-plat .save{margin-top:6px;border:1px solid #bfdbfe;background:#eff6ff;color:#2563eb;' +
      'border-radius:7px;padding:5px 12px;font-size:12px;cursor:pointer}' +
      '.wb-hide{display:none !important}' +
      // 员工角色：同步条是管理员专属，用 body class 强制隐藏（元素是异步创建的，逐个 hide 会失效）
      'body.wb-role-user #wb-syncbar,body.wb-role-user #conn-sync{display:none !important}' +
      '.wb-data-tip{position:fixed;left:50%;transform:translateX(-50%);top:58px;z-index:10020;' +
      'width:min(1080px,calc(100vw - 48px));box-sizing:border-box;display:flex;align-items:flex-start;gap:12px;' +
      'background:#fffbeb;border:1px solid #fcd34d;border-radius:12px;padding:11px 14px;' +
      'box-shadow:0 8px 24px rgba(180,120,0,.10);font-family:inherit}' +
      '.wb-data-tip .wdt-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}' +
      '.wb-data-tip b{font-size:13px;color:#92400e}' +
      '.wb-data-tip span{font-size:12.5px;color:#78350f;line-height:1.6}' +
      '.wb-data-tip .wdt-act{display:flex;align-items:center;gap:8px;flex:none}' +
      '.wb-data-tip .wdt-btn{border:1px solid #f59e0b;background:#fff;color:#92400e;border-radius:8px;' +
      'padding:6px 12px;font-size:12.5px;cursor:pointer;text-decoration:none;white-space:nowrap}' +
      '.wb-data-tip .wdt-btn:hover{background:#fef3c7}' +
      '.wb-data-tip .wdt-btn:disabled{opacity:.6;cursor:default}' +
      '.wb-data-tip .wdt-x{border:none;background:transparent;color:#b45309;font-size:18px;line-height:1;' +
      'cursor:pointer;padding:0 2px}' +
      '@media (max-width:720px){.wb-data-tip{top:52px;flex-direction:column}.wb-data-tip .wdt-act{align-self:flex-end}}';
    document.head.appendChild(s);
  }

  /* ---------- 登录 ---------- */
  function showLogin(msg) {
    if (document.getElementById('wb-login')) {
      if (msg) setMsg(msg, false);
      return;
    }
    var box = document.createElement('div');
    box.className = 'wb-login';
    box.id = 'wb-login';
    box.innerHTML =
      '<div class="wb-login-card">' +
      '<h2>任务安排工作台</h2>' +
      '<div class="sub">员工的待办与审批由钉钉身份自动识别；管理员用密码进入配置。</div>' +
      '<div class="wb-login-tabs"><span class="on" data-tab="user">员工（钉钉登录）</span>' +
      '<span data-tab="admin">管理员</span></div>' +
      '<div id="wb-tab-user">' +
      '<button id="wb-ding-login">' + (_inDing() ? '用钉钉身份进入' : '钉钉扫码登录') + '</button>' +
      '<div class="sub" id="wb-ding-hint" style="margin-top:10px">' +
      (_inDing() ? '正在识别你的钉钉身份…' : '当前是普通浏览器（无钉钉容器），免登会报 4040，已自动改用扫码登录。') +
      '</div>' +
      (_inDing() ? '' :
        '<div class="sub" id="wb-cb-hint" style="margin-top:12px;padding-top:10px;border-top:1px dashed #e5e7eb;' +
        'font-size:11.5px;line-height:1.7;color:#9ca3af">' +
        '扫码报「redirect_uri 参数错误」= 下边这个回调地址没在钉钉后台登记。<br>' +
        '回调地址：<code id="wb-cb-uri" style="color:#6b7280"></code>' +
        '<button type="button" id="wb-cb-copy" style="margin-left:6px;padding:1px 8px;font-size:11px;' +
        'border:1px solid #e5e7eb;background:#fff;border-radius:6px;cursor:pointer">复制</button>' +
        '<button type="button" id="wb-cb-detail" style="margin-left:4px;padding:1px 8px;font-size:11px;' +
        'border:1px solid #e5e7eb;background:#fff;border-radius:6px;cursor:pointer">怎么配</button>' +
        '<div id="wb-cb-more" class="wb-hide" style="margin-top:6px;color:#6b7280;line-height:1.9">' +
        '① 打开 open-dev.dingtalk.com → 你的应用 → <b>登录与分享</b><br>' +
        '② 在「回调域名」里新增：<code id="wb-cb-domain" style="color:#2563eb"></code><br>' +
        '③ 保存并<b>发布应用版本</b>，再回来重新扫码。<br>' +
        '（只填协议+主机+端口，<b>不要带路径</b>；回调地址与它必须完全一致）' +
        '</div></div>') +
      '</div>' +
      '<div id="wb-tab-admin" class="wb-hide">' +
      '<label>管理密码</label><input type="password" id="wb-pwd" placeholder="部署时设置的管理员密码">' +
      '<button id="wb-admin-login">进入管理后台</button>' +
      '</div>' +
      '<div class="wb-login-msg" id="wb-login-msg"></div>' +
      '</div>';
    document.body.appendChild(box);

    var tabUser = box.querySelector('#wb-tab-user');
    var tabAdmin = box.querySelector('#wb-tab-admin');
    box.querySelectorAll('.wb-login-tabs span').forEach(function (sp) {
      sp.addEventListener('click', function () {
        box.querySelectorAll('.wb-login-tabs span').forEach(function (x) { x.className = ''; });
        sp.className = 'on';
        var isUser = sp.getAttribute('data-tab') === 'user';
        tabUser.className = isUser ? '' : 'wb-hide';
        tabAdmin.className = isUser ? 'wb-hide' : '';
      });
    });

    // 扫码登录回调信息：与后端 _base_url() 保持同一规则（优先 public_url，其次当前 Host）
    var cbUri = box.querySelector('#wb-cb-uri');
    if (cbUri) {
      var configured = (window.__WB_PUBLIC_URL || '').replace(/\/+$/, '');
      var base = configured || (location.protocol + '//' + location.host);
      var full = base + '/auth/dingtalk/callback';
      cbUri.textContent = full;
      var dm = box.querySelector('#wb-cb-domain');
      if (dm) dm.textContent = base.replace(/\/+$/, '');
      box.querySelector('#wb-cb-copy').addEventListener('click', function () {
        var done = function () { setMsg('回调地址已复制：' + full, true); };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(full).then(done, function () { setMsg(full, true); });
        } else {
          setMsg(full, true);
        }
      });
      box.querySelector('#wb-cb-detail').addEventListener('click', function () {
        var more = box.querySelector('#wb-cb-more');
        if (more) more.className = more.className.indexOf('wb-hide') >= 0 ? '' : 'wb-hide';
      });
    }

    box.querySelector('#wb-admin-login').addEventListener('click', function () {
      var pw = box.querySelector('#wb-pwd').value.trim();
      if (!pw) return setMsg('请输入管理密码', false);
      setMsg('登录中…', true);
      window.WB_API('POST', '/auth/admin', { password: pw }).then(function (r) {
        setToken(r.token);
        location.reload();
      }).catch(function (e) { setMsg(e.message || '登录失败', false); });
    });

    box.querySelector('#wb-ding-login').addEventListener('click', function () {
      // 非钉钉容器：JSAPI 一定返回 4040，直接走扫码登录，避免这个报错
      if (!_inDing()) {
        setMsg('正在跳转钉钉扫码登录…', true);
        location.href = '/auth/qr';
        return;
      }
      setMsg('正在获取钉钉身份…', true);
      dingLogin().catch(function (e) { setMsg(e.message || '钉钉登录失败', false); });
    });

    if (msg) setMsg(msg, false);

    // 环境诊断：把「为什么点不动」直接写在登录卡上
    if (!_inDing()) {
      setHint('当前是普通浏览器，没有钉钉容器，所以免登会报 4040 notInDingTalk。点上面的按钮用手机钉钉扫码即可登录；' +
        '若在钉钉客户端内打开本页，则自动免登。');
    } else if (!CORP) {
      setHint('本页缺少钉钉企业身份（CorpId）：请管理员先在「平台配置 → 钉钉」填写并保存。');
    }
  }

  function _inDing() { return /DingTalk/i.test(navigator.userAgent || ''); }

  function setHint(t) {
    var h = document.getElementById('wb-ding-hint');
    if (h) h.textContent = t;
  }

  function setMsg(t, ok) {
    var m = document.getElementById('wb-login-msg');
    if (!m) return;
    m.textContent = t;
    m.className = 'wb-login-msg' + (ok ? ' ok' : '');
  }

  /* ---------- 钉钉免登 ---------- */
  function loadJS(src) {
    return new Promise(function (res, rej) {
      if (document.querySelector('script[src="' + src + '"]')) return res();
      var s = document.createElement('script');
      s.src = src;
      s.onload = res;
      s.onerror = function () { rej(new Error('加载钉钉 JSAPI 失败')); };
      document.head.appendChild(s);
    });
  }

  function dingLogin() {
    var inDing = _inDing();
    var pageUrl = location.href.split('#')[0];

    // 0) 不在钉钉容器内：dd.config 必然报 4040 notInDingTalk，这里直接拦掉
    if (!inDing) {
      return Promise.reject(new Error(
        '当前不在钉钉客户端内（JSAPI 4040 · notInDingTalk）：免登只能在钉钉里发起，请改用钉钉扫码登录'));
    }

    // 1) 先问服务端：凭证齐不齐、签名能不能给（缺失时直接给出可操作的提示）
    return window.WB_API('GET', '/api/jsapi-config?url=' + encodeURIComponent(pageUrl))
      .then(function (cfg) {
        if (!cfg || cfg.ready === false) {
          throw new Error(cfg && cfg.reason ? cfg.reason : '钉钉免登未就绪');
        }
        if (!cfg.agentId) {
          throw new Error('缺少 AgentId：请在管理员页 → 平台配置 → 钉钉 填写微应用 AgentId');
        }
        return loadJS('https://g.alicdn.com/dingding/dingtalk-jsapi/3.0.25/dingtalk.open.js')
          .then(function () {
            return new Promise(function (resolve, reject) {
              if (!window.dd) return reject(new Error('当前环境未注入钉钉 JSAPI'));
              // dd.env 是最可靠的容器判定：notInDingTalk 时不要调 dd.config
              var env = (dd.env && dd.env.platform) || '';
              if (env === 'notInDingTalk' || env === 'notInDingtalk') {
                return reject(new Error('JSAPI 4040：当前页面不在钉钉客户端内，请改用钉钉扫码登录'));
              }
              // 2) dd.config 是免登的前提（签名必须与当前页面 URL 完全一致）
              dd.config({
                agentId: cfg.agentId,
                corpId: cfg.corpId,
                timeStamp: cfg.timeStamp,
                nonceStr: cfg.nonceStr,
                signature: cfg.signature,
                jsApiList: cfg.jsApiList || ['runtime.permission.requestAuthCode']
              });
              dd.error(function (err) {
                var m = (err && (err.errorMessage || err.message)) || 'dd.config 校验失败';
                var code = (err && (err.errorCode || err.code)) || '';
                if (/notInDingTalk/i.test(m) || String(code) === '4040') {
                  m = 'JSAPI 4040：当前页面不在钉钉客户端内。请用手机钉钉扫码登录，或在钉钉里重新打开本页';
                } else if (/signature/i.test(m)) {
                  m += '（签名不匹配：请确认钉钉应用首页地址与本页地址完全一致，含端口）';
                }
                reject(new Error(m));
              });
              dd.ready(function () {
                dd.runtime.permission.requestAuthCode({
                  corpId: cfg.corpId,
                  onSuccess: function (info) {
                    window.WB_API('POST', '/auth/dingtalk', { code: info.code })
                      .then(function (r) {
                        setToken(r.token);
                        location.reload();
                      })
                      .catch(reject);
                  },
                  onFail: function (err) {
                    var msg = (err && (err.errorMessage || err.message)) || '';
                    if (!inDing) {
                      msg = '当前不在钉钉客户端内，免登无法发起。' +
                        (msg ? '（' + msg + '）' : '') + '请用钉钉扫码进入，或联系管理员用管理密码登录。';
                    }
                    reject(new Error(msg || '钉钉授权失败'));
                  }
                });
              });
              // 兜底：8 秒内 dd.ready 不回调，通常是在普通浏览器打开
              setTimeout(function () {
                if (!window.__wbDingReady) {
                  window.__wbDingReady = true;
                  if (!inDing) {
                    reject(new Error('未检测到钉钉运行环境：请在钉钉客户端内打开本页，' +
                      '或用管理员密码登录'));
                  }
                }
              }, 8000);
            });
          });
      });
  }

  /* ---------- 角色渲染 ---------- */
  function applyRole(me) {
    ME = me;
    var admin = me.kind === 'admin';
    document.body.classList.toggle('wb-role-user', !admin);
    // 顶栏身份 + 一键退出（不依赖弹层，弹层再出问题也能退出）
    var top = document.querySelector('.topbar-right');
    if (!top) {
      top = document.createElement('div');
      top.className = 'topbar-right';
      top.style.cssText = 'position:fixed;top:12px;right:16px;z-index:10040;display:flex;align-items:center;gap:8px';
      document.body.appendChild(top);
    }
    if (top && !document.getElementById('wb-ident')) {
      var chip = document.createElement('div');
      chip.className = 'wb-ident';
      chip.id = 'wb-ident';
      chip.textContent = (admin ? '🛡 ' : '👤 ') + (me.name || (admin ? '管理员' : '我'));
      chip.title = admin ? '管理员：可管理人员与平台配置' : '当前登录身份';
      chip.addEventListener('click', function () { openPanel(); });
      top.insertBefore(chip, top.firstChild);

      var lo = document.createElement('button');
      lo.id = 'wb-logout-top';
      lo.className = 'wba-topbtn';
      lo.textContent = '退出';
      lo.title = '退出当前登录';
      lo.addEventListener('click', function (e) { e.stopPropagation(); logout(); });
      top.insertBefore(lo, chip.nextSibling);

      var av = top.querySelector('.avatar');
      if (av) av.textContent = (me.name || (admin ? '管' : '我')).slice(0, 1);
    }
    if (!admin) {
      // 员工：隐藏管理组与配置入口
      document.querySelectorAll('[data-nav="settings"],[data-nav="apps"],[data-nav="categories"]').forEach(function (a) {
        var grp = a.closest('.side-group');
        a.classList.add('wb-hide');
        if (grp && !grp.querySelector('a:not(.wb-hide)')) grp.classList.add('wb-hide');
      });
      var bar = document.getElementById('wb-syncbar');
      if (bar) bar.classList.add('wb-hide');
      // 阻止直接访问配置路由
      window.addEventListener('hashchange', guardRoute);
      guardRoute();
    }
    dataTip(me);
  }

  /* ---------- 空数据诊断条：说明“为什么没数据”，而不是干空白 ---------- */
  function dataTip(me) {
    window.WB_API('GET', '/api/status').then(function (st) {
      var old = document.getElementById('wb-data-tip');
      if (old) old.remove();
      var total = 0, c = st.counts || {};
      Object.keys(c).forEach(function (k) { total += (Number(c[k]) || 0); });
      if (total > 0) return;

      var msg = '';
      (st.logs || []).some(function (l) {
        if (l.kind === 'sync' && l.msg) { msg = l.msg; return true; }
        return false;
      });
      var apply = (msg.match(/https:\/\/open-dev\.dingtalk\.com\/appscope\/apply\?[^\s；，]+/) || [''])[0];
      var admin = me.kind === 'admin';

      var head, body;
      var gTotal = Number(st.globalTotal || 0);
      if (!admin && gTotal > 0) {
        head = '你的账号没有匹配到任何待办 / 审批';
        body = '系统里已经有 ' + gTotal + ' 条数据，但你的钉钉账号还没和平台账号对上。' +
          '请让管理员在「人员管理」里核对你的手机号 / 邮箱绑定。';
      } else if (/qyapi_work_record/.test(msg)) {
        head = '钉钉「OA 审批」权限未开通，所以一条审批都同步不进来';
        body = '这是唯一的硬阻塞：应用没有 qyapi_work_record 权限，同步时审批被整批跳过。' +
          '开通 → 重新发布应用 → 点一次「立即同步」，数据就会进来。';
      } else if (/没有可同步的钉钉成员/.test(msg)) {
        head = '人员库里还没有可同步的成员';
        body = '先去「人员管理」拉取钉钉通讯录，再回首页同步。';
      } else if (msg) {
        head = '最近一次同步没有取到数据';
        body = msg.slice(0, 220);
      } else {
        head = admin ? '工作台还没有任何数据' : '你的账号暂时没有待办 / 审批数据';
        body = admin ? '还没成功同步过：点右上「立即同步」，或先在「平台配置」里确认凭证。'
          : '如果管理员刚开通权限，需要他点一次「立即同步」后你才会看到数据。';
      }

      var box = document.createElement('div');
      box.className = 'wb-data-tip';
      box.id = 'wb-data-tip';
      box.innerHTML =
        '<div class="wdt-body"><b>' + esc(head) + '</b><span>' + esc(body) + '</span></div>' +
        '<div class="wdt-act">' +
        (apply ? '<a class="wdt-btn" href="' + esc(apply) + '" target="_blank" rel="noopener">去开通权限</a>' : '') +
        (admin ? '<button class="wdt-btn" id="wdt-sync">立即同步</button>' : '') +
        '<button class="wdt-x" id="wdt-x" title="关闭">×</button>' +
        '</div>';
      document.body.appendChild(box);
      box.querySelector('#wdt-x').addEventListener('click', function () { box.remove(); });
      var sb = box.querySelector('#wdt-sync');
      if (sb) sb.addEventListener('click', function () {
        sb.disabled = true; sb.textContent = '同步中…';
        window.WB_API('POST', '/api/sync', {}).then(function () {
          setTimeout(function () { location.reload(); }, 2500);
        }).catch(function (e) { sb.disabled = false; sb.textContent = '立即同步'; alert(e.message); });
      });
    }).catch(function () { });
  }

  function guardRoute() {
    if (!ME || ME.kind === 'admin') return;
    if (/^#\/(settings|apps|categories)/.test(location.hash)) location.hash = '#/';
  }

  /* ---------- 管理面板 ---------- */
  function openPanel() {
    var old = document.getElementById('wb-panel');
    if (old) old.remove();
    var admin = ME && ME.kind === 'admin';
    var box = document.createElement('div');
    box.className = 'wba-mask';
    box.id = 'wb-panel';
    box.innerHTML = '<div class="wba-card"><button class="wb-close" id="wb-p-close">关闭</button>' +
      '<h3>' + (admin ? '管理员控制台' : '当前身份') + '</h3>' +
      '<div class="m-sub">' + (admin ? '人员绑定、平台凭证都在这里；员工页面看不到这些入口。'
        : '你看到的待办/审批只属于你自己。') + '</div>' +
      '<div id="wb-p-body"></div></div>';
    document.body.appendChild(box);
    box.querySelector('#wb-p-close').addEventListener('click', function () { box.remove(); });
    box.addEventListener('click', function (e) { if (e.target === box) box.remove(); });

    var body = box.querySelector('#wb-p-body');
    if (!admin) {
      body.innerHTML = '<div class="wb-tools"><button id="wb-logout" class="wba-danger">退出登录</button></div>';
      body.querySelector('#wb-logout').addEventListener('click', logout);
      return;
    }
    body.innerHTML =
      '<div class="wb-tools">' +
      '<button data-act="persons">人员管理</button>' +
      '<button data-act="platforms">平台配置</button>' +
      '<button data-act="ding">钉钉免登自检</button>' +
      '<button data-act="pwd">修改密码</button>' +
      '<button id="wb-logout" class="wba-danger">退出登录</button>' +
      '</div><div id="wb-p-view"><div class="m-sub">选择上面的功能开始。</div></div>';
    body.querySelector('#wb-logout').addEventListener('click', logout);
    body.querySelectorAll('[data-act]').forEach(function (b) {
      b.addEventListener('click', function () {
        var act = b.getAttribute('data-act');
        if (act === 'persons') renderPersons(body.querySelector('#wb-p-view'));
        if (act === 'platforms') renderPlatforms(body.querySelector('#wb-p-view'));
        if (act === 'ding') renderDingCheck(body.querySelector('#wb-p-view'));
        if (act === 'pwd') renderPwd(body.querySelector('#wb-p-view'));
      });
    });
    renderPersons(body.querySelector('#wb-p-view'));
  }

  function logout() {
    // 1) 通知服务端销毁会话（失败也继续）
    try { window.WB_API('GET', '/auth/logout').catch(function () { }); } catch (e) { }
    // 2) 清空本地会话与缓存
    setToken('');
    try {
      localStorage.removeItem(TOKEN_KEY);
      sessionStorage.clear();
    } catch (e) { }
    ME = null;
    // 3) 去掉 URL 里的 ?token=（否则刷新会从地址栏把身份带回来，导致"退不出去"）
    var clean = location.pathname + location.hash;
    setTimeout(function () { location.replace(clean); }, 200);
  }

  var STATUS_TEXT = { bound: ['已绑定', 'ok'], conflict: ['冲突', 'err'], unmatched: ['未匹配', 'warn'], manual: ['手动', 'mute'], disabled: ['已停用', 'mute'] };

  function renderPersons(view) {
    view.innerHTML = '<div class="m-sub">加载中…</div>';
    window.WB_API('GET', '/api/persons').then(function (d) {
      var rep = d.report || {};
      var plats = d.platforms || {};
      var rows = (d.persons || []).map(function (p) {
        var st = STATUS_TEXT[p.status] || [p.status || '-', 'mute'];
        var badges = ['dingtalk', 'feishu', 'kingdee', 'yonyou', 'zentao', 'github', 'gitlab', 'gitea'].map(function (k) {
          var a = (p.accounts || {})[k];
          return a ? '<span class="wb-badge ok">' + k + '：' + esc(a.account_id) + '</span>'
            : '<span class="wb-badge mute">' + k + ' —</span>';
        }).join('');
        return '<tr><td>' + esc(p.name || '-') + '</td><td>' + badges + '</td>' +
          '<td>' + (p.phones || []).map(esc).join('<br>') + '</td>' +
          '<td>' + (p.emails || []).map(esc).join('<br>') + '</td>' +
          '<td><span class="wb-badge ' + st[1] + '">' + st[0] + '</span></td>' +
          '<td><button class="wb-badge" data-bind="' + p.person_id + '">绑账号</button> ' +
          '<button class="wb-badge" data-del="' + p.person_id + '">删除</button></td></tr>';
      }).join('');
      view.innerHTML =
        '<div class="wb-tools">' +
        '<button data-pull="dingtalk">拉取钉钉通讯录</button>' +
        '<button data-pull="feishu">拉取飞书通讯录</button>' +
        '<button data-pull="kingdee">拉取金蝶用户</button>' +
        '<button data-pull="zentao">拉取禅道用户</button>' +
        '<button data-pull="gitlab">拉取GitLab用户</button>' +
        '<button data-pull="gitea">拉取Gitea用户</button>' +
        '<button data-match="1">重新自动匹配</button>' +
        '<button data-add="1">手动加人</button></div>' +
        '<div class="m-sub">共 ' + (rep.total || 0) + ' 人 · 已绑定 ' + (rep.bound || 0) +
        ' · 冲突 ' + (rep.conflict || 0) + ' · 未匹配 ' + (rep.unmatched || 0) +
        ' ｜ 平台账号：' + Object.keys(rep.by_platform || {}).map(function (k) {
          return k + ' ' + rep.by_platform[k];
        }).join(' / ') + '</div>' +
        '<table class="wb-table"><thead><tr><th>姓名</th><th>平台账号</th><th>手机</th>' +
        '<th>邮箱</th><th>状态</th><th>操作</th></tr></thead><tbody>' + rows + '</tbody></table>';

      view.querySelectorAll('[data-pull]').forEach(function (b) {
        b.addEventListener('click', function () {
          b.disabled = true; b.textContent = '拉取中…';
          window.WB_API('POST', '/api/persons', { action: 'pull', platform: b.getAttribute('data-pull') })
            .then(function (r) { alert('拉取 ' + r.pulled + ' 人，合并 ' + (r.match && r.match.merged || 0) + ' 组'); renderPersons(view); })
            .catch(function (e) { alert(e.message); b.disabled = false; b.textContent = '重试'; });
        });
      });
      view.querySelector('[data-match]').addEventListener('click', function () {
        window.WB_API('POST', '/api/persons', { action: 'match' }).then(function (r) {
          alert('合并 ' + r.merged + ' 组，冲突 ' + r.conflicts + ' 人'); renderPersons(view);
        }).catch(function (e) { alert(e.message); });
      });
      view.querySelector('[data-add]').addEventListener('click', function () {
        var name = prompt('姓名'); if (!name) return;
        window.WB_API('POST', '/api/persons', { action: 'add', name: name }).then(function () { renderPersons(view); });
      });
      view.querySelectorAll('[data-bind]').forEach(function (b) {
        b.addEventListener('click', function () {
          var plat = prompt('平台：dingtalk / feishu / kingdee / github', 'github'); if (!plat) return;
          var acc = prompt(plat === 'github' ? 'GitHub 用户名' : '该平台的账号ID'); if (!acc) return;
          window.WB_API('POST', '/api/persons', {
            action: 'bind', person_id: b.getAttribute('data-bind'), platform: plat, account_id: acc
          }).then(function () { renderPersons(view); }).catch(function (e) { alert(e.message); });
        });
      });
      view.querySelectorAll('[data-del]').forEach(function (b) {
        b.addEventListener('click', function () {
          if (!confirm('确定删除该人员？其数据将不再被员工看到。')) return;
          window.WB_API('POST', '/api/persons', { action: 'delete', person_id: b.getAttribute('data-del') })
            .then(function () { renderPersons(view); });
        });
      });
    }).catch(function (e) { view.innerHTML = '<div class="m-sub">加载失败：' + esc(e.message) + '</div>'; });
  }

  var PLAT_LABEL = { dingtalk: '钉钉', feishu: '飞书', github: 'GitHub', gitlab: 'GitLab',
                     gitea: 'Gitea/Gogs', zentao: '禅道', kingdee: '金蝶云星空', yonyou: '用友', wecom: '企业微信' };

  function renderPlatforms(view) {
    view.innerHTML = '<div class="m-sub">加载中…</div>';
    window.WB_API('GET', '/api/platforms').then(function (d) {
      view.innerHTML = (d.platforms || []).map(function (p) {
        var fields = (p.fields || []).map(function (f) {
          var v = f.has ? '' : (f.value || '');
          var ph = f.has ? '已保存（留空不修改）' : (f.value || '');
          return '<div class="row"><span>' + esc(f.label) + '</span>' +
            '<input data-p="' + p.platform + '" data-k="' + f.key + '" value="' + esc(v) + '" placeholder="' + esc(ph) + '"></div>';
        }).join('');
        return '<div class="wb-plat"><h4>' + (PLAT_LABEL[p.platform] || p.platform) +
          ' <span class="wb-badge ' + (p.ready ? 'ok' : 'warn') + '">' + (p.ready ? '已配置' : '未配置') + '</span></h4>' +
          fields + '<button class="save" data-save="' + p.platform + '">保存</button></div>';
      }).join('');
      view.querySelectorAll('[data-save]').forEach(function (b) {
        b.addEventListener('click', function () {
          var plat = b.getAttribute('data-save');
          var body = { platform: plat };
          view.querySelectorAll('input[data-p="' + plat + '"]').forEach(function (i) {
            body[i.getAttribute('data-k')] = i.value.trim();
          });
          b.disabled = true; b.textContent = '保存中…';
          window.WB_API('POST', '/api/platforms', body).then(function () {
            b.textContent = '已保存';
            setTimeout(function () { renderPlatforms(view); }, 600);
          }).catch(function (e) { alert(e.message); b.disabled = false; b.textContent = '保存'; });
        });
      });
    }).catch(function (e) { view.innerHTML = '<div class="m-sub">加载失败：' + esc(e.message) + '</div>'; });
  }

  function renderDingCheck(view) {
    view.innerHTML = '<div class="m-sub">自检中…</div>';
    window.WB_API('POST', '/api/dingtalk-check', {}).then(function (d) {
      var rows = (d.steps || []).map(function (s) {
        return '<tr><td>' + esc(s.name) + '</td>' +
          '<td><span class="wb-badge ' + (s.ok ? 'ok' : 'err') + '">' + (s.ok ? '正常' : '异常') + '</span></td>' +
          '<td>' + esc(s.detail || '') + '</td>' +
          '<td class="m-sub">' + esc(s.fix || '') + '</td></tr>';
      }).join('');
      view.innerHTML =
        '<div class="wb-tools"><button id="wb-ding-recheck">重新自检</button></div>' +
        '<div class="m-sub">' + (d.ok ? '全部通过：钉钉客户端内打开本页即可自动免登。'
          : esc(d.hint || '有未通过的项，按「修复建议」处理。')) + '</div>' +
        '<table class="wb-table"><thead><tr><th>检查项</th><th>结果</th><th>实际值</th>' +
        '<th>修复建议</th></tr></thead><tbody>' + rows + '</tbody></table>' +
        '<div class="m-sub" style="margin-top:12px">另外两项在钉钉后台，无法自动检测：' +
        '① 应用「首页地址」= 本页地址（含端口）② 权限管理里已开通「JSAPI 鉴权 / 成员信息读」并已发布版本。</div>';
      var rb = view.querySelector('#wb-ding-recheck');
      if (rb) rb.addEventListener('click', function () { renderDingCheck(view); });
    }).catch(function (e) {
      view.innerHTML = '<div class="m-sub">自检失败：' + esc(e.message) + '</div>';
    });
  }

  function renderPwd(view) {
    view.innerHTML = '<div style="max-width:340px">' +
      '<label style="font-size:12px;color:#4b5563">原密码</label>' +
      '<input type="password" id="wb-old" style="width:100%;padding:8px;border:1px solid #e5e7eb;border-radius:8px;margin:4px 0 10px">' +
      '<label style="font-size:12px;color:#4b5563">新密码（≥6 位）</label>' +
      '<input type="password" id="wb-new" style="width:100%;padding:8px;border:1px solid #e5e7eb;border-radius:8px;margin:4px 0 10px">' +
      '<button class="save" id="wb-pwd-save">保存</button></div>';
    view.querySelector('#wb-pwd-save').addEventListener('click', function () {
      window.WB_API('POST', '/api/admin/password', {
        old: view.querySelector('#wb-old').value, new: view.querySelector('#wb-new').value
      }).then(function () { alert('已修改，请重新登录'); setToken(''); location.reload(); })
        .catch(function (e) { alert(e.message); });
    });
  }

  function esc(s) {
    return String(s === undefined || s === null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  /* ---------- 启动 ---------- */
  function boot() {
    css();
    // 支持 ?token=xxx 直接带会话（便于联调 / 从钉钉深链进入）
    try {
      var qs = new URLSearchParams(location.search);
      if (qs.get('token')) {
        setToken(qs.get('token'));
        qs.delete('token');
        var clean = location.pathname + (qs.toString() ? ('?' + qs.toString()) : '') + location.hash;
        history.replaceState(null, '', clean);
      }
    } catch (e) { }
    // 匿名只拿到「有没有配钉钉」的布尔，企业标识（CorpId/AppKey）登录后才下发
    _fetch('/api/ping', { headers: {} }).then(function (r) { return r.json(); }).then(function (d) {
      CORP = d.ding_login ? '1' : '';
      window.__WB_PUBLIC_URL = d.public_url || '';
      if (token()) return;
      showLogin();
      // 钉钉客户端内：自动尝试免登，同事点开即进
      if (_inDing() && CORP) {
        setTimeout(function () {
          var b = document.getElementById('wb-ding-login');
          if (b) b.click();
        }, 300);
      }
    }).catch(function () {
      if (!token()) showLogin();
    });
    if (!token()) return;
    window.WB_API('GET', '/auth/me').then(function (me) {
      applyRole(me);
    }).catch(function () {
      setToken('');
      showLogin('登录已过期，请重新登录');
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
