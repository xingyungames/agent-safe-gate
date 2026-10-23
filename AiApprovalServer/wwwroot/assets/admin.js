//管理后台页面
(function () {
  'use strict';

  var el = U.el;
  var state = {
    tab: 'overview',
    pendingToken: null,   //邮件里的审批令牌
    pendingTaskId: null,
    currentUser: null,
    feishuUserTableUrl: null,   //用户表飞书地址
    feishuLastResult: null      //上次同步结果
  };

  //可进后台的角色
  var ADMIN_ROLES = { admin: 1, superadmin: 1 };
  var HTTPS_PREFIX = 'https://';

  //权限判断
  function hasPerm(key) {
    var list = (state.currentUser && state.currentUser.permissions) || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].key === key) return true;
    }
    return false;
  }

  //新建账号可选角色
  function roleOptionsForCreate() {
    var options = [['employee', '员工'], ['finance', '财务部']];
    //管理员角色需授权
    if (hasPerm('account.grant_admin')) options.push(['admin', '管理员']);
    return options;
  }

  //角色中文名
  function roleLabelOf(role) {
    if (role === 'superadmin') return '超级管理员';
    if (role === 'admin') return '管理员';
    if (role === 'finance') return '财务部';
    if (role === 'employee') return '员工';
    return role;
  }

  //角色徽标配色
  function roleBadgeClass(role) {
    if (role === 'superadmin') return 'bad';
    if (role === 'admin') return 'warn';
    if (role === 'finance') return 'info';
    return 'ok';
  }

  //页面启动
  function boot() {
    var params = new URLSearchParams(location.search);
    state.pendingTaskId = params.get('task');
    state.pendingToken = params.get('token');
    if (state.pendingTaskId) {
      document.getElementById('login-banner').textContent =
        '你通过邮件审批链接进入（任务 ' + state.pendingTaskId + '）。请先以管理员身份登录，随后可校验签名令牌并批准。';
    }

    if (API.isLoggedIn()) {
      API.get('/api/auth/me').then(function (res) {
        if (!ADMIN_ROLES[res.data.role]) {
          API.clear();
          U.toast('该账号不是管理员或超级管理员，无权访问后台', 'bad');
          showLogin();
          return;
        }
        state.currentUser = res.data;
        enterAdmin();
      }).catch(function () { showLogin(); });
    } else {
      showLogin();
    }
  }

  //切回登录页
  function showLogin() {
    document.getElementById('login-view').hidden = false;
    document.getElementById('admin-view').hidden = true;
    document.getElementById('userbox').hidden = true;
  }

  //进入后台
  function enterAdmin() {
    document.getElementById('login-view').hidden = true;
    document.getElementById('admin-view').hidden = false;
    document.getElementById('userbox').hidden = false;
    document.getElementById('whoami').textContent = state.currentUser.displayName + '（' + state.currentUser.userName + '）';
    switchTab(state.pendingTaskId ? 'pending' : 'overview');
  }

  //提交管理员登录
  function login() {
    var userName = document.getElementById('login-user').value.trim();
    var password = document.getElementById('login-pass').value;
    if (!userName || !password) { U.toast('请输入用户名与口令', 'warn'); return; }

    API.public('/api/auth/login', { userName: userName, password: password }).then(function (res) {
      if (!ADMIN_ROLES[res.data.user.role]) {
        U.toast('账号 ' + res.data.user.userName + ' 不是管理员或超级管理员，已拒绝进入后台', 'bad');
        return;
      }
      API.setSession(res.data);
      document.getElementById('login-pass').value = '';
      state.currentUser = res.data.user;
      U.toast('管理员登录成功', 'ok');
      enterAdmin();
    }).catch(function (err) { U.toast(err.message, 'bad'); });
  }

  //渲染数据表格
  function table(columns, rows, onRowClick) {
    var wrap = el('div', null);
    if (!rows.length) { wrap.appendChild(el('div', 'empty', '暂无数据')); return wrap; }

    var t = el('table');
    var thead = el('thead');
    var tr = el('tr');
    columns.forEach(function (c) { tr.appendChild(el('th', null, c.title)); });
    thead.appendChild(tr);
    t.appendChild(thead);

    var tbody = el('tbody');
    rows.forEach(function (row) {
      var rowNode = el('tr');
      columns.forEach(function (c) {
        var td = el('td');
        var value = c.render ? c.render(row) : row[c.key];
        if (value instanceof Node) td.appendChild(value);
        else td.textContent = value === undefined || value === null || value === '' ? '-' : String(value);
        rowNode.appendChild(td);
      });
      if (onRowClick) {
        rowNode.classList.add('clickable');
        rowNode.addEventListener('click', function () { onRowClick(row); });
      }
      tbody.appendChild(rowNode);
    });
    t.appendChild(tbody);
    wrap.appendChild(t);
    return wrap;
  }

  //生成状态徽标
  function badge(text, cls) { return el('span', 'badge ' + (cls || 'info'), text); }

  //带标题的分节
  function section(title, node) {
    var wrap = el('div', null);
    wrap.appendChild(el('div', 'hint', title));
    wrap.appendChild(node);
    wrap.appendChild(el('div', 'divider'));
    return wrap;
  }

  //打开弹层
  function openModal(title, contentNode, footerNode) {
    var host = U.clear(document.getElementById('modal-host'));
    var mask = el('div', 'modal-mask');
    var modal = el('div', 'modal');
    var head = el('div', 'modal-head');
    head.appendChild(el('span', null, title));
    head.appendChild(el('span', 'spacer'));

    var close = el('button', 'small ghost', '关闭');
    close.addEventListener('click', function () { U.clear(host); });
    head.appendChild(close);

    var body = el('div', 'modal-body');
    body.appendChild(contentNode);
    modal.appendChild(head);
    modal.appendChild(body);

    if (footerNode) {
      var foot = el('div', 'modal-foot');
      foot.appendChild(footerNode);
      modal.appendChild(foot);
    }

    mask.appendChild(modal);
    mask.addEventListener('click', function (e) { if (e.target === mask) U.clear(host); });
    host.appendChild(mask);
  }

  //渲染 JSON 块
  function jsonBlock(value) {
    var text = typeof value === 'string' ? value : U.json(value);
    return el('pre', 'code', U.unescapeUnicode(text));
  }

  //报销单 AI 等级徽标
  function aiLevelBadge(level) {
    var n = Number(level) || 0;
    var cls = (n >= 1 && n <= 4) ? 'lvl' + n : 'lvl0';
    var text = n === 1 ? '1 正常' : n === 2 ? '2 注意' : n === 3 ? '3 危险' : n === 4 ? '4 严重' : '未评分';
    return badge(text, cls);
  }

  //加载报销单 AI 建议（只读：本页没有任何执行入口）
  function loadInvoiceAi(body) {
    U.clear(body);

    var note = el('div', 'banner info');
    note.textContent = '管理台只展示 AI 建议。系统不存在由 AI 触发审批或执行的接口：' +
      '审批必须由人或财务在各自界面点击，且每次写操作都会独立留快照与审计。';
    body.appendChild(note);

    var bar = el('div', 'row');
    var refreshBtn = el('button', 'small', '刷新 AI 预审');
    refreshBtn.type = 'button';
    bar.appendChild(refreshBtn);

    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'grow';
    input.maxLength = 200;
    input.placeholder = '让 AI 按需求筛单（例如：找出所有没问题的单子）';
    bar.appendChild(input);

    var findBtn = el('button', 'small ghost', 'AI 筛单');
    findBtn.type = 'button';
    bar.appendChild(findBtn);
    body.appendChild(bar);

    var stat = el('div', 'ai-stat');
    body.appendChild(stat);

    var tableHost = el('div', 'list');
    tableHost.textContent = '加载中…';
    body.appendChild(tableHost);

    var pickHost = el('div', 'ai-pick-list');
    body.appendChild(pickHost);

    function renderScores(rows) {
      U.clear(tableHost);
      if (!rows.length) {
        tableHost.appendChild(el('div', 'empty', '暂无单据'));
        return;
      }

      var t = el('table');
      var thead = el('thead');
      var hr = el('tr');
      ['单号', '申请人', '类型', '金额', 'AI 等级', 'AI 理由', '规则摘要', '状态'].forEach(function (x) {
        hr.appendChild(el('th', null, x));
      });
      thead.appendChild(hr);
      t.appendChild(thead);

      var tbody = el('tbody');
      rows.forEach(function (r) {
        var tr = el('tr');
        tr.appendChild(el('td', null, r.billNo || '-'));
        tr.appendChild(el('td', null, r.applicant || '-'));
        tr.appendChild(el('td', null, r.rtype || '-'));
        tr.appendChild(el('td', null, r.amount === undefined ? '-' : String(r.amount)));

        var lvlCell = el('td');
        lvlCell.appendChild(aiLevelBadge(r.hasScore ? r.finalLevel : 0));
        tr.appendChild(lvlCell);

        tr.appendChild(el('td', null, r.aiReason || '-'));
        tr.appendChild(el('td', null, r.ruleSummary || '-'));
        tr.appendChild(el('td', null, r.status || '-'));
        tbody.appendChild(tr);
      });
      t.appendChild(tbody);
      tableHost.appendChild(t);
    }

    function load(refresh) {
      tableHost.textContent = '加载中…';
      API.get('/api/admin/invoice-ai/scores' + (refresh ? '?refresh=true' : '')).then(function (res) {
        var d = res.data || {};
        stat.textContent = '已评分 ' + (d.scoredCount || 0) + ' / ' + (d.total || 0) + ' 单' +
          (d.aiError ? '　AI 提示：' + d.aiError : '');
        renderScores(d.rows || []);
      }).catch(function (err) {
        tableHost.textContent = '加载失败：' + err.message;
      });
    }

    refreshBtn.addEventListener('click', function () { load(true); });

    findBtn.addEventListener('click', function () {
      var q = input.value.trim();
      if (q.length < 2) { U.toast('请先描述要筛的单（至少 2 个字）', 'warn'); return; }

      findBtn.disabled = true;
      API.get('/api/admin/invoice-ai/find?q=' + encodeURIComponent(q)).then(function (res) {
        var d = res.data || {};
        U.clear(pickHost);
        pickHost.appendChild(el('div', 'ai-stat', d.note ? String(d.note) : ''));

        var picked = d.picked || [];
        picked.forEach(function (p) {
          var row = el('div', 'ai-pick-item');
          var txt = el('div', 'txt');
          txt.appendChild(el('div', null, p.summary || p.billNo || (p.recordId || '-')));
          if (p.reason) txt.appendChild(el('div', 'why', 'AI：' + p.reason));
          row.appendChild(txt);

          var lv = el('div');
          lv.appendChild(aiLevelBadge(p.level));
          row.appendChild(lv);
          pickHost.appendChild(row);
        });

        (d.dropped || []).forEach(function (x) {
          pickHost.appendChild(el('div', 'hint', '已剔除 ' + (x.recordId || '') + '：' + (x.droppedReason || '')));
        });

        U.toast('AI 已给出 ' + picked.length + ' 条建议（未执行任何操作）', 'info');
      }).catch(function (err) {
        U.toast('AI 筛单失败：' + err.message, 'bad');
      }).then(function () { findBtn.disabled = false; });
    });

    load(false);
  }

  //切换标签
  function switchTab(name) {    state.tab = name;
    Array.prototype.forEach.call(document.querySelectorAll('#admin-tabs .tab'), function (t) {
      t.classList.toggle('active', t.getAttribute('data-tab') === name);
    });

    var body = document.getElementById('admin-body');
    body.textContent = '加载中…';

    switch (name) {
      case 'overview': loadOverview(body); break;
      case 'pending': loadPending(body); break;
      case 'tasks': loadAllTasks(body); break;
      case 'versions': loadVersions(body); break;
      case 'users': loadUsers(body); break;
      case 'audit': loadAudit(body); break;
      case 'outbox': loadOutbox(body); break;
      case 'datasets': loadDatasets(body); break;
      case 'invoiceai': loadInvoiceAi(body); break;
      case 'security': loadSecurity(body); break;
      default: body.textContent = '未知标签';
    }
  }

  //加载总览
  function loadOverview(body) {
    API.get('/api/admin/overview').then(function (res) {
      var d = res.data;
      U.clear(body);

      var stats = el('div', 'stat-grid');
      [
        ['待人工审批', d.pendingCount, 'warn'],
        ['执行中', d.executingCount, 'info'],
        ['已执行', d.executedCount, 'ok'],
        ['被拒绝', d.rejectedCount, 'bad'],
        ['执行失败', d.failedCount, 'muted'],
        ['账号总数', d.userCount, 'info'],
        ['版本记录', d.versionCount, 'info'],
        ['审计条目', d.auditCount, 'info'],
        ['出站通知', d.outboxCount, 'info'],
        ['活跃封禁 IP', d.security.activeBans, 'bad']
      ].forEach(function (item) {
        var box = el('div', 'stat');
        box.appendChild(el('div', 'label', item[0]));
        box.appendChild(el('div', 'value', String(item[1])));
        stats.appendChild(box);
      });
      body.appendChild(stats);

      var chain = el('div', 'banner ' + (d.auditChainOk ? 'ok' : 'bad'),
        '审计链校验：' + d.auditChainMessage);
      body.appendChild(chain);

      var ai = el('div', 'banner ' + (d.aiConfigured ? 'ok' : 'warn'),
        d.aiConfigured
          ? ('AI 已配置（' + d.aiModel + '）：上游意图解析 + 隔离式安全审核 + 管理端只读分析')
          : 'AI 未配置：当前使用本地意图解析，所有变更类操作强制人工审批（防线不降级）');
      body.appendChild(ai);

      body.appendChild(el('div', 'divider'));
      body.appendChild(el('div', 'hint', '最新待审批任务'));
      body.appendChild(table([
        { title: '任务号', key: 'id' },
        { title: '提交人', key: 'userName' },
        { title: '意图', key: 'intent' },
        { title: '工具', key: 'tool' },
        { title: '风险', render: function (r) { return badge(r.riskLabel, U.riskClass(r.riskLevel)); } },
        { title: '提交时间', render: function (r) { return U.fmtTime(r.createdAt); } }
      ], d.newestPending || [], function (row) { openTaskDetail(row.id); }));
    }).catch(function (err) { body.textContent = '加载失败：' + err.message; });
  }

  //加载待审批任务
  function loadPending(body) {
    API.get('/api/admin/tasks?status=PENDING_APPROVAL&limit=100').then(function (res) {
      U.clear(body);
      var list = res.data || [];

      body.appendChild(el('div', 'banner', list.length
        ? ('共 ' + list.length + ' 条待审批任务。点击任意一行查看完整上下文（含用户原始输入）后决定。')
        : '当前没有待审批任务。'));

      body.appendChild(table([
        { title: '任务号', key: 'id' },
        { title: '提交人', render: function (r) { return r.userName + '（' + r.userRoleLabel + '）'; } },
        { title: '意图摘要', key: 'intent' },
        { title: '工具', key: 'tool' },
        { title: '风险', render: function (r) { return badge(r.riskLabel, U.riskClass(r.riskLevel)); } },
        { title: '结构审核', render: function (r) { return badge(r.review.verdict, r.review.verdict === 'deny' ? 'bad' : 'info'); } },
        { title: '注入特征', render: function (r) { return (r.injectionFlags || []).length ? badge('有', 'bad') : '无'; } },
        { title: '提交时间', render: function (r) { return U.fmtTime(r.createdAt); } }
      ], list, function (row) { openTaskDetail(row.id); }));

      //邮件审批入口提示
      if (state.pendingTaskId) {
        var found = list.filter(function (t) { return t.id === state.pendingTaskId; })[0];
        if (found) {
          body.insertBefore(el('div', 'banner warn',
            '你正在通过邮件链接处理任务 ' + found.id + '。点击该任务可查看完整上下文；'
            + (state.pendingToken ? '本次链接带签名令牌，可在弹层里用“通过签名令牌批准”。' : '本次链接不带令牌，请用常规“同意/打回”。')),
            body.firstChild);
        }
      }
    }).catch(function (err) { body.textContent = '加载失败：' + err.message; });
  }

  //加载全部任务
  function loadAllTasks(body) {
    var row = el('div', 'row');
    var select = el('select');
    ['', 'PENDING_APPROVAL', 'EXECUTING', 'EXECUTED', 'REJECTED_RULE', 'REJECTED_AI', 'REJECTED_ADMIN', 'FAILED', 'EXPIRED']
      .forEach(function (s) {
        var opt = el('option', null, s === '' ? '全部状态' : s);
        opt.value = s;
        select.appendChild(opt);
      });
    row.appendChild(select);

    var keyword = el('input', 'grow');
    keyword.type = 'text';
    keyword.placeholder = '按提交人过滤（用户名）';
    row.appendChild(keyword);

    var btn = el('button', 'primary small', '查询');
    row.appendChild(btn);
    body.appendChild(row);
    body.appendChild(el('div', 'divider'));

    var host = el('div', null, '点击“查询”加载数据');
    body.appendChild(host);

    //按条件查询
    function run() {
      host.textContent = '加载中…';
      var url = '/api/admin/tasks?limit=200&status=' + encodeURIComponent(select.value);
      API.get(url).then(function (res) {
        U.clear(host);
        var keywordValue = keyword.value.trim().toLowerCase();
        var list = (res.data || []).filter(function (t) {
          return !keywordValue || (t.userName || '').toLowerCase().indexOf(keywordValue) >= 0;
        });

        host.appendChild(table([
          { title: '任务号', key: 'id' },
          { title: '提交人', key: 'userName' },
          { title: '意图', key: 'intent' },
          { title: '工具', key: 'tool' },
          { title: '风险', render: function (r) { return badge(r.riskLabel, U.riskClass(r.riskLevel)); } },
          { title: '状态', render: function (r) { return badge(r.statusLabel, U.statusClass(r.status)); } },
          { title: '提交时间', render: function (r) { return U.fmtTime(r.createdAt); } },
          { title: '裁决人', key: 'decidedBy' }
        ], list, function (r) { openTaskDetail(r.id); }));
      }).catch(function (err) { host.textContent = '加载失败：' + err.message; });
    }

    btn.addEventListener('click', run);
    run();
  }

  //审批详情
  //打开任务审批弹层
  function openTaskDetail(taskId) {
    API.get('/api/admin/tasks/' + encodeURIComponent(taskId)).then(function (res) {
      var task = res.data.task;
      var versions = res.data.versions || [];
      var body = el('div', null);

      body.appendChild(section('用户原始输入（服务端解密展示，邮件通知里不含此内容）',
        jsonBlock(task.rawInput || '（无）')));

      body.appendChild(section('意图摘要（上游 AI 生成，可能被污染）',
        jsonBlock(task.intent)));

      body.appendChild(section('规范化动作（执行端唯一依据）',
        jsonBlock(task.action)));

      body.appendChild(section('规则引擎结论',
        jsonBlock({ decision: task.rule.decision, requiresApproval: task.rule.requiresApproval, reasons: task.rule.reasons })));

      body.appendChild(section('AI 安全审核（物理隔离：看不到上面的原始输入）',
        jsonBlock(task.review)));

      if ((task.injectionFlags || []).length) {
        var flags = el('div', 'banner bad', '输入侧命中注入特征：' + task.injectionFlags.join('、'));
        body.appendChild(flags);
      }

      body.appendChild(section('执行结果',
        jsonBlock(task.result || '（尚未执行）')));

      if (versions.length) {
        var vWrap = el('div', null);
        versions.forEach(function (v) {
          vWrap.appendChild(el('div', 'hint', v.operation + ' ' + v.target + '　+' + v.addedLines + ' / -' + v.removedLines +
            (v.reverted ? '（已回退）' : '')));
          vWrap.appendChild(jsonBlock(v.diff));
        });
        body.appendChild(section('本次执行产生的版本与差异', vWrap));
      }

      var reason = el('textarea');
      reason.placeholder = '审批意见（必填，不少于 2 个字符）：同意说明依据，打回说明理由';
      reason.maxLength = 200;
      body.appendChild(el('div', 'divider'));
      body.appendChild(el('div', 'hint', '审批意见'));
      body.appendChild(reason);

      var footer = el('div', 'row tight');

      if (task.status === 'PENDING_APPROVAL') {
        var approve = el('button', 'ok', '同意并执行');
        approve.addEventListener('click', function () {
          if (reason.value.trim().length < 2) { U.toast('请填写审批意见', 'warn'); return; }
          approve.disabled = true;
          API.post('/api/admin/tasks/' + encodeURIComponent(task.id) + '/approve', { reason: reason.value.trim() })
            .then(function (r) { U.toast(r.data.message, 'ok'); U.clear(document.getElementById('modal-host')); refreshCurrentTab(); })
            .catch(function (e) { approve.disabled = false; U.toast(e.message, 'bad'); });
        });

        var reject = el('button', 'danger', '打回');
        reject.addEventListener('click', function () {
          if (reason.value.trim().length < 2) { U.toast('请填写打回理由', 'warn'); return; }
          reject.disabled = true;
          API.post('/api/admin/tasks/' + encodeURIComponent(task.id) + '/reject', { reason: reason.value.trim() })
            .then(function (r) { U.toast(r.data.message, 'ok'); U.clear(document.getElementById('modal-host')); refreshCurrentTab(); })
            .catch(function (e) { reject.disabled = false; U.toast(e.message, 'bad'); });
        });

        footer.appendChild(approve);
        footer.appendChild(reject);

        if (state.pendingToken && state.pendingTaskId === task.id) {
          var byToken = el('button', 'primary', '通过邮件签名令牌批准');
          byToken.addEventListener('click', function () {
            byToken.disabled = true;
            API.post('/api/admin/tasks/approve-by-token', {
              taskId: task.id,
              token: state.pendingToken,
              reason: reason.value.trim() || '通过邮件签名链接批准'
            }).then(function (r) {
              U.toast(r.data.message, 'ok');
              state.pendingToken = null;
              U.clear(document.getElementById('modal-host'));
              refreshCurrentTab();
            }).catch(function (e) { byToken.disabled = false; U.toast(e.message, 'bad'); });
          });
          footer.appendChild(byToken);
        }
      } else {
        footer.appendChild(el('span', 'hint', '该任务当前状态：' + task.statusLabel + '，不能再审批。'));
      }

      openModal('任务审批上下文 · ' + task.id, body, footer);
    }).catch(function (err) { U.toast(err.message, 'bad'); });
  }

  //版本与回退
  //加载版本记录
  function loadVersions(body) {
    var row = el('div', 'row');
    var userInput = el('input', 'grow');
    userInput.type = 'text';
    userInput.placeholder = '按用户 ID 过滤（留空看全部）';
    row.appendChild(userInput);
    var kindSelect = el('select');
    [['', '全部类型'], ['invoice', '报销单'], ['file', '文件'], ['notice', '通知']].forEach(function (pair) {
      var opt = el('option', null, pair[1]);
      opt.value = pair[0];
      kindSelect.appendChild(opt);
    });
    row.appendChild(kindSelect);
    var btn = el('button', 'primary small', '查询');
    row.appendChild(btn);
    body.appendChild(row);
    body.appendChild(el('div', 'hint',
      '回退基于执行前保存的全量快照与反向操作数据；回退操作本身同样会留快照，可再次回退（撤销误回退）。' +
      '报销单版本（kind 为 invoice）请用「按日志回退」，它会在飞书侧还原单据并把原动作写进审计。'));
    body.appendChild(el('div', 'divider'));

    var host = el('div', null, '加载中…');
    body.appendChild(host);

    function run() {
      host.textContent = '加载中…';
      API.get('/api/admin/versions?limit=100&userId=' + encodeURIComponent(userInput.value.trim())).then(function (res) {
        U.clear(host);
        var list = res.data || [];
        if (kindSelect.value) {
          list = list.filter(function (v) { return v.kind === kindSelect.value; });
        }
        if (!list.length) { host.appendChild(el('div', 'empty', '没有版本记录')); return; }

        list.forEach(function (v) {
          var card = el('div', 'card');
          card.className = 'list-item';
          var title = el('div', 'title');
          title.appendChild(el('span', null, v.operation + ' ' + v.target));
          title.appendChild(document.createTextNode('  '));
          title.appendChild(badge(v.kind + ' / ' + (v.reverted ? '已回退' : '可回退'), v.reverted ? 'bad' : 'ok'));
          card.appendChild(title);
          card.appendChild(el('div', 'meta', '用户 ' + v.userName + '（' + v.userId + '）　+' + v.addedLines + ' / -' + v.removedLines +
            '　' + U.fmtTime(v.createdAt) + '　任务 ' + v.taskId));

          var pre = el('pre', 'code');
          (v.diff || '').split('\n').slice(0, 80).forEach(function (line) {
            pre.appendChild(el('span', line.charAt(0) === '+' ? 'diff-add' : line.charAt(0) === '-' ? 'diff-del' : null, line + '\n'));
          });
          card.appendChild(pre);

          var rowNode = el('div', 'row tight');
          var detail = el('button', 'small ghost', '查看完整差异');
          detail.addEventListener('click', function () { openModal('版本差异 ' + v.id, jsonBlock(v.diff)); });
          rowNode.appendChild(detail);

          if (!v.reverted && v.kind === 'invoice') {
            var invoiceRollback = el('button', 'small danger', '按日志回退');
            invoiceRollback.addEventListener('click', function () { confirmInvoiceRollback(v); });
            rowNode.appendChild(invoiceRollback);
          } else if (!v.reverted) {
            var rollback = el('button', 'small danger', '一键回退');
            rollback.addEventListener('click', function () { confirmRollback(v); });
            rowNode.appendChild(rollback);
          }
          card.appendChild(rowNode);
          host.appendChild(card);
        });
      }).catch(function (err) { host.textContent = '加载失败：' + err.message; });
    }

    btn.addEventListener('click', run);
    kindSelect.addEventListener('change', run);
    run();
  }

  //报销单回退确认
  function confirmInvoiceRollback(version) {
    var recordId = String(version.target || '').replace('invoice:', '');
    var box = el('div', null);
    box.appendChild(el('div', 'banner warn',
      '即将按这条日志回退报销单（原动作：' + version.operation + '）。服务端会在飞书侧还原单据，并写入审计哈希链；' +
      '如果原动作是删除，会以原字段重建记录，重建后的单号会由飞书重新生成。'));
    box.appendChild(el('div', 'hint', '单据记录号：' + (recordId || version.target)));
    box.appendChild(jsonBlock(version.diff));

    var footer = el('div', 'row tight');
    var confirm = el('button', 'danger', '确认回退');
    confirm.addEventListener('click', function () {
      confirm.disabled = true;
      API.post('/api/invoice/rollback', { versionId: version.id })
        .then(function (res) {
          U.toast((res.data && res.data.message) || '已回退', 'ok');
          U.clear(document.getElementById('modal-host'));
          refreshCurrentTab();
        }).catch(function (err) { confirm.disabled = false; U.toast(err.message, 'bad'); });
    });
    footer.appendChild(confirm);
    openModal('报销单回退确认 · ' + version.id, box, footer);
  }

  //通用回退确认
  function confirmRollback(version) {
    var body = el('div', null);
    body.appendChild(el('div', 'banner warn',
      '即将把「' + version.operation + ' ' + version.target + '」回退到执行前的状态。回退会写审计日志，并生成新的版本记录。'));
    body.appendChild(jsonBlock(version.diff));

    var reason = el('textarea');
    reason.maxLength = 200;
    reason.placeholder = '回退原因（必填）';
    body.appendChild(el('div', 'hint', '回退原因'));
    body.appendChild(reason);

    var footer = el('div', 'row tight');
    var confirm = el('button', 'danger', '确认回退');
    confirm.addEventListener('click', function () {
      if (reason.value.trim().length < 2) { U.toast('请填写回退原因', 'warn'); return; }
      confirm.disabled = true;
      API.post('/api/admin/versions/' + encodeURIComponent(version.id) + '/rollback', { reason: reason.value.trim() })
        .then(function (res) {
          U.toast(res.data.message, 'ok');
          U.clear(document.getElementById('modal-host'));
          refreshCurrentTab();
        }).catch(function (err) { confirm.disabled = false; U.toast(err.message, 'bad'); });
    });
    footer.appendChild(confirm);
    openModal('回退确认 · ' + version.id, body, footer);
  }

  //账号管理
  //加载账号列表
  function loadUsers(body) {
    API.get('/api/admin/users').then(function (res) {
      U.clear(body);
      var users = res.data || [];

      body.appendChild(el('div', 'hint',
        '账号权威保存在本机 users.json；飞书用户表只是「看起来一致」的镜像，镜像失败不影响账号使用。' +
        '角色差异：员工只能提交与查询自己的报销，财务部可审核与修改，管理员可删除与回退，超级管理员额外可授予管理员与删除账号。'));

      body.appendChild(createAccountForm());
      body.appendChild(feishuSyncPanel());

      if (hasPerm('account.list')) {
        body.appendChild(table([
          { title: '用户名', key: 'userName' },
          { title: '姓名', key: 'displayName' },
          { title: '工号', render: function (r) { return r.employeeId || '-'; } },
          { title: '角色', render: function (r) { return badge(r.roleLabel || roleLabelOf(r.role), roleBadgeClass(r.role)); } },
          { title: '来源', render: function (r) { return badge(originLabel(r.origin), r.origin === 'bootstrap' ? 'warn' : 'muted'); } },
          { title: '状态', render: function (r) { return r.disabled ? badge('已停用', 'bad') : r.locked ? badge('锁定中', 'warn') : badge('正常', 'ok'); } },
          { title: '最后登录', render: function (r) { return U.fmtTime(r.lastLoginAt) + (r.lastLoginIp ? '（' + r.lastLoginIp + '）' : ''); } },
          { title: '飞书镜像', render: function (r) { return mirrorCell(r); } },
          { title: '操作', render: function (r) { return userActions(r); } }
        ], users));
      } else {
        body.appendChild(el('div', 'empty', '当前角色无权查看账号列表'));
      }
    }).catch(function (err) { body.textContent = '加载失败：' + err.message; });
  }

  //账号来源标签
  var ORIGIN_LABELS = { local: '本机创建', 'feishu-sync': '飞书同步', bootstrap: '初始超管' };

  //来源转中文
  function originLabel(origin) {
    return ORIGIN_LABELS[origin] || '本机创建';
  }

  //创建账号表单
  function createAccountForm() {
    var wrap = el('div', null);
    var createRow = el('div', 'row');
    var name = el('input'); name.type = 'text'; name.placeholder = '用户名'; name.maxLength = 32;
    var display = el('input'); display.type = 'text'; display.placeholder = '姓名'; display.maxLength = 32;
    var pass = el('input'); pass.type = 'password'; pass.placeholder = '初始口令（至少 10 位，含三类字符）'; pass.maxLength = 128;
    var email = el('input'); email.type = 'email'; email.placeholder = '邮箱（可选）'; email.maxLength = 128;
    var role = el('select');
    roleOptionsForCreate().forEach(function (pair) {
      var opt = el('option', null, pair[1]);
      opt.value = pair[0];
      role.appendChild(opt);
    });
    var createBtn = el('button', 'primary small', '创建账号');

    [name, display, pass, email, role, createBtn].forEach(function (n) { createRow.appendChild(n); });
    wrap.appendChild(el('div', 'hint',
      '创建账号（口令强度：至少 10 位，且包含大写字母 / 小写字母 / 数字 / 符号中的三类；角色为「员工」或「财务部」时任何管理员都能创建，' +
      '创建「管理员」只有超级管理员可以）。工号由服务端分配。'));
    wrap.appendChild(createRow);
    wrap.appendChild(el('div', 'divider'));

    createBtn.addEventListener('click', function () {
      createBtn.disabled = true;
      API.post('/api/admin/users', {
        userName: name.value.trim(),
        displayName: display.value.trim(),
        password: pass.value,
        role: role.value,
        email: email.value.trim()
      }).then(function (r) {
        createBtn.disabled = false;
        var data = r.data || {};
        U.toast(data.message || '账号已创建', 'ok');
        pass.value = '';
        if (data.mirrorDemoPassword) {
          showDemoPasswordModal('新账号 ' + (data.user ? data.user.userName : '') + ' 的飞书演示口令',
            data.mirrorDemoPassword, data.mirrorNote || '');
        } else if (data.mirrorNote) {
          U.toast(data.mirrorNote, 'warn');
        }
        refreshCurrentTab();
      }).catch(function (e) { createBtn.disabled = false; U.toast(e.message, 'bad'); });
    });

    return wrap;
  }

  //飞书账号同步
  //只放行 https 地址
  function safeHttpsUrl(value) {
    if (typeof value !== 'string') return '';
    var text = value.trim();
    if (text.length <= HTTPS_PREFIX.length) return '';
    if (text.toLowerCase().slice(0, HTTPS_PREFIX.length) !== HTTPS_PREFIX) return '';
    return text;
  }

  //设置飞书链接
  function applyFeishuLink(link, url) {
    if (!url) { link.removeAttribute('href'); link.hidden = true; return; }
    link.href = url;
    link.hidden = false;
  }

  //用户表地址缓存
  function loadFeishuUserTableLink(link) {
    if (typeof state.feishuUserTableUrl === 'string') {
      applyFeishuLink(link, state.feishuUserTableUrl);
      return;
    }
    API.get('/api/invoice/meta').then(function (res) {
      state.feishuUserTableUrl = safeHttpsUrl((res.data || {}).userTableUrl);
      applyFeishuLink(link, state.feishuUserTableUrl);
    }).catch(function () { applyFeishuLink(link, ''); });
  }

  var FEISHU_ACTION_LABELS = { created: '新建', adopted: '认领', exists: '已存在', skipped: '跳过' };

  //同步处置转中文
  function feishuActionLabel(action) {
    return FEISHU_ACTION_LABELS[action] || (action || '-');
  }

  //同步处置配色
  function feishuActionClass(action) {
    if (action === 'created') return 'ok';
    if (action === 'adopted') return 'info';
    if (action === 'skipped') return 'warn';
    return 'muted';
  }

  //数字缺失显示短横
  function feishuCount(value) {
    return (typeof value === 'number') ? String(value) : '-';
  }

  //账号同步面板
  function feishuSyncPanel() {
    var wrap = el('div', null);
    wrap.appendChild(el('div', 'hint', '飞书账号同步'));
    wrap.appendChild(el('div', 'hint',
      '本机账号库是唯一权威，飞书用户表只是镜像。同步只把对面用户表里存在、本机还没有的账号补进来；' +
      '本机已存在的账号（含超级管理员）一律不改动，角色与口令也不会被对面覆盖。'));
    wrap.appendChild(el('div', 'hint', '预览只读，不写入任何账号；同步是写操作，会留审计。'));

    var row = el('div', 'row tight');

    var previewBtn = el('button', 'small', '预览对面账号');
    previewBtn.id = 'admin-feishu-preview';
    row.appendChild(previewBtn);

    var syncBtn = el('button', 'primary small', '同步缺失账号');
    syncBtn.id = 'admin-feishu-sync';
    row.appendChild(syncBtn);

    var link = el('a', 'hint', '在飞书中打开用户表');
    link.id = 'admin-feishu-user-table-link';
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.hidden = true;
    row.appendChild(link);

    wrap.appendChild(row);
    loadFeishuUserTableLink(link);

    var host = el('div', null);
    host.id = 'admin-feishu-result';
    var rowsHost = el('div', null);
    rowsHost.id = 'admin-feishu-rows';
    var itemsHost = el('div', null);
    itemsHost.id = 'admin-feishu-items';
    host.appendChild(rowsHost);
    host.appendChild(itemsHost);
    wrap.appendChild(host);

    //放回上次同步结果
    if (state.feishuLastResult) renderFeishuSyncResult(itemsHost, state.feishuLastResult);

    previewBtn.addEventListener('click', function () {
      loadFeishuRows(rowsHost);
      runFeishuSync(itemsHost, true, [previewBtn, syncBtn]);
    });

    syncBtn.addEventListener('click', function () {
      var agreed = window.confirm('即将把对面用户表里本机还没有的账号补进本机账号库：只补本机没有的账号，' +
        '已存在的账号不会被覆盖。是否继续？');
      if (!agreed) return;
      U.clear(rowsHost);
      runFeishuSync(itemsHost, false, [previewBtn, syncBtn]);
    });

    wrap.appendChild(el('div', 'divider'));
    return wrap;
  }

  //预览对面用户表
  function loadFeishuRows(host) {
    U.clear(host);
    host.appendChild(el('div', 'hint', '正在读取对面的用户表…'));
    API.get('/api/admin/users/feishu-rows').then(function (res) {
      var data = res.data || {};
      U.clear(host);
      if (!data.configured) {
        host.appendChild(el('div', 'banner warn', data.message || '未配置飞书，无法读取对面的用户表'));
        return;
      }
      var rows = data.rows || [];
      host.appendChild(el('div', 'hint',
        '对面用户表共 ' + feishuCount(typeof data.count === 'number' ? data.count : rows.length) + ' 行（只读展示）'));
      host.appendChild(table([
        { title: '用户名', key: 'userName' },
        { title: '姓名', key: 'displayName' },
        { title: '工号', key: 'employeeId' },
        { title: '角色', key: 'roleLabel' },
        { title: '状态', key: 'statusLabel' },
        { title: '对面口令', render: function (r) { return r.hasPassword ? badge('已设置', 'ok') : badge('未设置', 'muted'); } },
        { title: '本机账号', render: function (r) { return r.existsLocally ? badge('已存在', 'info') : badge('缺失', 'warn'); } },
        { title: '创建时间', render: function (r) { return U.fmtTime(r.createdAt); } },
        { title: '最后登录', render: function (r) { return U.fmtTime(r.lastLoginAt); } }
      ], rows));
    }).catch(function (err) {
      U.clear(host);
      var message = (err && err.message) || '请求失败';
      host.appendChild(el('div', 'banner ' + feishuErrorClass(err), feishuErrorText(err, message)));
    });
  }

  //错误提示配色
  function feishuErrorClass(err) {
    return (err && (err.status === 409 || err.status === 403)) ? 'warn' : 'bad';
  }

  //错误文案
  function feishuErrorText(err, message) {
    if (err && err.status === 403) return '权限不足：' + message;
    return message;
  }

  //展示飞书错误
  function reportFeishuError(host, err) {
    var message = (err && err.message) || '请求失败';
    U.clear(host);
    if (err && err.status === 401) {
      host.appendChild(el('div', 'banner warn', '登录状态已失效，请重新登录'));
      return;
    }
    host.appendChild(el('div', 'banner ' + feishuErrorClass(err), feishuErrorText(err, message)));
    U.toast(feishuErrorText(err, message), (err && (err.status === 409 || err.status === 403)) ? 'warn' : 'bad');
  }

  //执行预览或同步
  function runFeishuSync(host, dryRun, buttons) {
    buttons.forEach(function (b) { b.disabled = true; });
    U.clear(host);
    host.appendChild(el('div', 'hint', dryRun ? '正在预览对面的账号…' : '正在同步缺失账号…'));

    API.post('/api/admin/users/sync-from-feishu', {
      dryRun: dryRun,
      reason: dryRun ? '管理后台预览飞书账号同步' : '管理后台同步缺失账号'
    }).then(function (res) {
      buttons.forEach(function (b) { b.disabled = false; });
      var data = res.data || {};
      state.feishuLastResult = data;
      renderFeishuSyncResult(host, data);
      U.toast(data.message || (dryRun ? '预览完成' : '同步完成'), 'ok');
      if (!dryRun) refreshCurrentTab();
    }).catch(function (err) {
      buttons.forEach(function (b) { b.disabled = false; });
      reportFeishuError(host, err);
    });
  }

  //渲染同步结果
  function renderFeishuSyncResult(host, data) {
    U.clear(host);
    if (!data || typeof data !== 'object') return;

    host.appendChild(el('div', 'banner ' + (data.dryRun ? 'warn' : 'ok'), data.message || '同步已完成'));
    host.appendChild(el('div', 'hint',
      '读取 ' + feishuCount(data.read) + ' 行　新建 ' + feishuCount(data.created) +
      '　认领 ' + feishuCount(data.adopted) + '　已存在 ' + feishuCount(data.exists) +
      '　跳过 ' + feishuCount(data.skipped) + '　需重置口令 ' + feishuCount(data.needsPasswordReset)));

    host.appendChild(table([
      { title: '用户名', key: 'userName' },
      { title: '姓名', key: 'displayName' },
      { title: '工号', key: 'employeeId' },
      { title: '角色', key: 'roleLabel' },
      { title: '处置', render: function (r) { return badge(feishuActionLabel(r.action), feishuActionClass(r.action)); } },
      { title: '说明', render: function (r) { return feishuItemDetail(r); } }
    ], data.items || []));
  }

  //拼接行说明
  function feishuItemDetail(item) {
    var detail = (item.detail === undefined || item.detail === null) ? '' : String(item.detail);
    if (item.needsPasswordReset && detail.indexOf('需管理员重置口令') < 0) {
      detail = detail ? detail + '（需管理员重置口令）' : '需管理员重置口令';
    }
    return detail;
  }

  //渲染镜像状态单元格
  function mirrorCell(user) {
    var wrap = el('div', null);
    var state_ = user.mirrorState || 'pending';
    var cls = state_ === 'synced' ? 'ok' : state_ === 'failed' ? 'bad' : state_ === 'skipped' ? 'muted' : 'warn';
    var label = state_ === 'synced' ? '已同步' : state_ === 'failed' ? '失败' : state_ === 'skipped' ? '已跳过' : '待处理';
    wrap.appendChild(badge(label, cls));
    if (user.mirrorError) {
      var summary = String(user.mirrorError);
      if (summary.length > 60) summary = summary.slice(0, 60) + '…';
      wrap.appendChild(el('div', 'meta', summary));
    }
    if (user.mirrorAt) wrap.appendChild(el('div', 'meta', U.fmtTime(user.mirrorAt)));
    wrap.appendChild(el('div', 'meta', '对面角色：' + (user.mirrorRoleLabel || '-')));
    return wrap;
  }

  //生成账号操作按钮
  function userActions(user) {
    var wrap = el('div', 'row tight');

    if (hasPerm('account.manage') || hasPerm('account.grant_admin')) {
      var roleSelect = el('select');
      roleOptionsForCreate().forEach(function (pair) {
        var opt = el('option', null, pair[1]);
        opt.value = pair[0];
        if (pair[0] === user.role) opt.selected = true;
        roleSelect.appendChild(opt);
      });
      if (user.role === 'superadmin') {
        var keep = el('option', null, '超级管理员');
        keep.value = 'superadmin';
        keep.selected = true;
        roleSelect.insertBefore(keep, roleSelect.firstChild);
      }
      roleSelect.addEventListener('click', function (e) { e.stopPropagation(); });

      var roleBtn = el('button', 'small', '改角色');
      roleBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        openRoleChangeModal(user, roleSelect.value);
      });

      wrap.appendChild(roleSelect);
      wrap.appendChild(roleBtn);
    }

    if (hasPerm('account.manage')) {
      var toggleBtn = el('button', 'small ' + (user.disabled ? 'ok' : 'danger'), user.disabled ? '启用' : '停用');
      toggleBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        API.post('/api/admin/users/' + encodeURIComponent(user.id) + '/status', {
          disabled: !user.disabled,
          reason: user.disabled ? '管理后台启用账号' : '管理后台停用账号'
        }).then(function (r) { U.toast(r.data.message, 'ok'); refreshCurrentTab(); })
          .catch(function (err) { U.toast(err.message, 'bad'); });
      });
      wrap.appendChild(toggleBtn);

      var resetBtn = el('button', 'small ghost', '重置口令');
      resetBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        openResetPasswordModal(user);
      });
      wrap.appendChild(resetBtn);

      var retryBtn = el('button', 'small ghost', '重试镜像');
      retryBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        retryBtn.disabled = true;
        API.post('/api/admin/users/' + encodeURIComponent(user.id) + '/mirror-retry', {})
          .then(function (r) {
            retryBtn.disabled = false;
            U.toast(r.data.message || '已重试镜像', r.data.ok ? 'ok' : 'warn');
            refreshCurrentTab();
          })
          .catch(function (err) { retryBtn.disabled = false; U.toast(err.message, 'bad'); });
      });
      wrap.appendChild(retryBtn);

      var demoBtn = el('button', 'small ghost', '查看演示口令');
      demoBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        demoBtn.disabled = true;
        API.get('/api/admin/users/' + encodeURIComponent(user.id) + '/mirror-password')
          .then(function (r) {
            demoBtn.disabled = false;
            var data = r.data || {};
            if (!data.demoPassword) { U.toast(data.hint || '该账号没有演示口令', 'warn'); return; }
            showDemoPasswordModal('账号 ' + user.userName + ' 的飞书演示口令', data.demoPassword, data.hint || '');
          })
          .catch(function (err) { demoBtn.disabled = false; U.toast(err.message, 'bad'); });
      });
      wrap.appendChild(demoBtn);
    }

    if (hasPerm('account.delete')) {
      var deleteBtn = el('button', 'small danger', '删除账号');
      deleteBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        openDeleteAccountModal(user);
      });
      wrap.appendChild(deleteBtn);
    }

    if (!wrap.firstChild) wrap.appendChild(el('span', 'hint', '无可用操作'));
    return wrap;
  }

  //改角色确认框
  function openRoleChangeModal(user, newRole) {
    if (!newRole || newRole === user.role) { U.toast('角色没有变化', 'warn'); return; }

    var box = el('div', null);
    box.appendChild(el('div', 'banner warn',
      '即将把账号 ' + user.userName + ' 的角色从「' + (user.roleLabel || roleLabelOf(user.role)) +
      '」改为「' + roleLabelOf(newRole) + '」。改角色会让该账号的旧令牌立即失效。'));
    var reason = el('textarea');
    reason.maxLength = 200;
    reason.placeholder = '变更理由（不少于 2 个字符）';
    box.appendChild(el('div', 'hint', '变更理由'));
    box.appendChild(reason);

    var footer = el('div', 'row tight');
    var ok = el('button', 'primary', '确认变更');
    ok.addEventListener('click', function () {
      if (reason.value.trim().length < 2) { U.toast('请填写变更理由', 'warn'); return; }
      ok.disabled = true;
      API.post('/api/admin/users/' + encodeURIComponent(user.id) + '/role', {
        role: newRole,
        reason: reason.value.trim()
      }).then(function (r) {
        U.toast(r.data.message, 'ok');
        U.clear(document.getElementById('modal-host'));
        refreshCurrentTab();
      }).catch(function (err) { ok.disabled = false; U.toast(err.message, 'bad'); });
    });
    footer.appendChild(ok);
    openModal('角色变更 · ' + user.userName, box, footer);
  }

  //重置口令弹层
  function openResetPasswordModal(user) {
    var box = el('div', null);
    box.appendChild(el('div', 'hint', '重置后该账号所有令牌立即失效；该账号在飞书用户表里的演示口令会重新生成。'));
    var input = el('input');
    input.type = 'password';
    input.maxLength = 128;
    input.placeholder = '新口令（至少 10 位，含三类字符）';
    var reason = el('input');
    reason.type = 'text';
    reason.maxLength = 200;
    reason.placeholder = '重置理由（可选，会记入审计）';
    box.appendChild(input);
    box.appendChild(reason);

    var footer = el('div', 'row tight');
    var ok = el('button', 'primary', '确认重置');
    ok.addEventListener('click', function () {
      ok.disabled = true;
      API.post('/api/admin/users/' + encodeURIComponent(user.id) + '/reset-password', {
        newPassword: input.value,
        reason: reason.value.trim()
      }).then(function (r) {
        ok.disabled = false;
        U.clear(document.getElementById('modal-host'));
        U.toast(r.data.message, 'ok');
        if (r.data.mirrorDemoPassword) {
          showDemoPasswordModal('账号 ' + user.userName + ' 的新飞书演示口令', r.data.mirrorDemoPassword, '');
        }
        refreshCurrentTab();
      }).catch(function (err) { ok.disabled = false; U.toast(err.message, 'bad'); });
    });
    footer.appendChild(ok);
    openModal('重置口令 · ' + user.userName, box, footer);
  }

  //删除账号弹层
  function openDeleteAccountModal(user) {
    var box = el('div', null);
    box.appendChild(el('div', 'banner bad',
      '删除账号会把它从本机权威账号库中移除，该账号立即无法登录。飞书用户表里的镜像行需要人工清理。'));
    box.appendChild(el('div', 'hint', '账号：' + user.userName + '（' + (user.roleLabel || roleLabelOf(user.role)) +
      '，工号 ' + (user.employeeId || '-') + '）'));

    var footer = el('div', 'row tight');
    var ok = el('button', 'danger', '确认删除');
    ok.addEventListener('click', function () {
      ok.disabled = true;
      API.post('/api/admin/users/' + encodeURIComponent(user.id) + '/delete', {})
        .then(function (r) {
          U.toast(r.data.message, 'ok');
          U.clear(document.getElementById('modal-host'));
          refreshCurrentTab();
        }).catch(function (err) { ok.disabled = false; U.toast(err.message, 'bad'); });
    });
    footer.appendChild(ok);
    openModal('删除账号 · ' + user.userName, box, footer);
  }

  //选中文本
  function selectText(node) {
    try {
      var range = document.createRange();
      range.selectNodeContents(node);
      var selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    } catch (e) { /* 无法选中时由用户手动复制 */ }
  }

  //演示口令弹层
  function showDemoPasswordModal(title, demoPassword, note) {
    var box = el('div', null);
    box.appendChild(el('div', 'banner warn',
      '这是写进飞书用户表的演示口令，可交给协作方登录他们的前端；与本系统的真实口令无关，也不影响本系统的登录校验。'));
    var code = el('pre', 'code', demoPassword);
    box.appendChild(code);
    if (note) box.appendChild(el('div', 'hint', note));
    box.appendChild(el('div', 'hint', '浏览器不允许自动复制时，可以手动选中上面的文本复制。'));

    var footer = el('div', 'row tight');
    var copy = el('button', 'primary small', '复制口令');
    copy.addEventListener('click', function () {
      var fallback = function () {
        selectText(code);
        U.toast('已选中口令文本，请按复制快捷键', 'warn');
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(demoPassword).then(function () {
          U.toast('演示口令已复制', 'ok');
        }).catch(fallback);
      } else {
        fallback();
      }
    });
    footer.appendChild(copy);
    openModal(title, box, footer);
  }

  //审计日志
  //加载审计日志
  function loadAudit(body) {
    var row = el('div', 'row');
    var action = el('input'); action.type = 'text'; action.placeholder = '按动作过滤（如 approval / honeypot / waf）';
    var actor = el('input'); actor.type = 'text'; actor.placeholder = '按操作者过滤';
    var btn = el('button', 'primary small', '查询');
    var verify = el('button', 'small', '校验哈希链');
    [action, actor, btn, verify].forEach(function (n) { row.appendChild(n); });
    body.appendChild(row);

    var chainHost = el('div', null);
    body.appendChild(chainHost);
    body.appendChild(el('div', 'divider'));

    var host = el('div', null, '加载中…');
    body.appendChild(host);

    //按条件查询
    function run() {
      host.textContent = '加载中…';
      API.get('/api/admin/audit?limit=200&action=' + encodeURIComponent(action.value.trim()) +
        '&actor=' + encodeURIComponent(actor.value.trim())).then(function (res) {
        U.clear(host);
        host.appendChild(table([
          { title: '序号', key: 'seq' },
          { title: '时间', render: function (r) { return U.fmtTime(r.time); } },
          { title: '操作者', render: function (r) { return (r.actorName || '-') + (r.actorRole ? '（' + r.actorRole + '）' : ''); } },
          { title: '来源 IP', key: 'ip' },
          { title: '动作', key: 'action' },
          { title: '目标', key: 'target' },
          { title: '结果', render: function (r) {
              var cls = r.outcome === 'SUCCESS' ? 'ok' : (r.outcome === 'FAIL' || r.outcome === 'BANNED' || r.outcome === 'DENIED') ? 'bad' : 'info';
              return badge(r.outcome, cls);
            } },
          { title: '详情', render: function (r) { return U.unescapeUnicode(r.detail || ''); } },
          { title: '哈希', key: 'hash' }
        ], U.unescapeDeep(res.data || [])));
      }).catch(function (err) { host.textContent = '加载失败：' + err.message; });
    }

    //校验审计哈希链
    verify.addEventListener('click', function () {
      API.get('/api/admin/audit/verify').then(function (res) {
        U.clear(chainHost);
        chainHost.appendChild(el('div', 'banner ' + (res.data.ok ? 'ok' : 'bad'), res.data.message));
      }).catch(function (err) { U.toast(err.message, 'bad'); });
    });

    btn.addEventListener('click', run);
    run();
  }

  //出站通知
  //加载出站通知
  function loadOutbox(body) {
    API.get('/api/admin/outbox?limit=100').then(function (res) {
      U.clear(body);
      body.appendChild(el('div', 'hint', 'SMTP 未启用时通知写入出站箱（不影响审批流程）；已发出的通知可以执行补偿事务撤回。'));
      body.appendChild(el('div', 'divider'));

      body.appendChild(table([
        { title: '编号', key: 'id' },
        { title: '时间', render: function (r) { return U.fmtTime(r.createdAt); } },
        { title: '收件人', key: 'to' },
        { title: '标题', key: 'subject' },
        { title: '关联任务', key: 'taskId' },
        { title: '提交人', key: 'ownerUserName' },
        { title: '状态', render: function (r) {
            var cls = r.status === 'SENT' ? 'ok' : r.status === 'RECALLED' ? 'bad' : r.status === 'FAILED' ? 'warn' : 'info';
            return badge(r.status, cls);
          } },
        { title: '操作', render: function (r) {
            var wrap = el('div', 'row tight');

            var view = el('button', 'small ghost', '查看');
            view.addEventListener('click', function (e) {
              e.stopPropagation();
              var node = el('div', null);
              node.appendChild(jsonBlock(r.body));
              if (r.approvalLink) {
                node.appendChild(el('div', 'hint', '审批链接（已签名、一次性、有有效期）'));
                node.appendChild(jsonBlock(r.approvalLink));
              }
              openModal('通知详情 ' + r.id, node);
            });
            wrap.appendChild(view);

            if (r.status === 'SENT' || r.status === 'QUEUED') {
              var recall = el('button', 'small danger', '撤回（补偿事务）');
              recall.addEventListener('click', function (e) {
                e.stopPropagation();
                API.post('/api/admin/outbox/' + encodeURIComponent(r.id) + '/recall', {})
                  .then(function (res2) { U.toast(res2.data.message, 'ok'); refreshCurrentTab(); })
                  .catch(function (err) { U.toast(err.message, 'bad'); });
              });
              wrap.appendChild(recall);
            }
            return wrap;
          } }
      ], res.data || []));
    }).catch(function (err) { body.textContent = '加载失败：' + err.message; });
  }

  //业务数据
  //加载数据集
  function loadDatasets(body) {
    var select = el('select');
    var host = el('div', null, '加载中…');
    var row = el('div', 'row');
    row.appendChild(el('span', 'hint', '数据集：'));
    row.appendChild(select);
    body.appendChild(row);
    body.appendChild(el('div', 'divider'));
    body.appendChild(host);

    //按名称加载
    function load(name) {
      host.textContent = '加载中…';
      API.get('/api/admin/datasets/' + encodeURIComponent(name)).then(function (res) {
        U.clear(host);
        host.appendChild(el('div', 'hint', res.data.label + '　共 ' + res.data.totalRows + ' 行　示意查询：' + res.data.query));

        var fields = res.data.fields || [];
        var columns = fields.map(function (f) {
          return {
            title: f.label + (f.writable ? '' : '（只读）'),
            render: function (r) { return r[f.name]; }
          };
        });
        host.appendChild(table(columns, res.data.rows || []));
      }).catch(function (err) { host.textContent = '加载失败：' + err.message; });
    }

    ['customers', 'orders', 'contracts'].forEach(function (name) {
      var opt = el('option', null, name);
      opt.value = name;
      select.appendChild(opt);
    });

    select.addEventListener('change', function () { load(select.value); });
    load('customers');
  }

  //安全态势
  //加载安全态势
  function loadSecurity(body) {
    API.get('/api/admin/security').then(function (res) {
      var s = res.data.snapshot;
      var limits = res.data.limits;
      U.clear(body);

      var stats = el('div', 'stat-grid');
      [
        ['WAF 拦截次数', s.wafBlocks],
        ['蜜罐命中次数', s.honeypotHits],
        ['登录失败次数', s.loginFailures],
        ['限流拒绝次数', s.rateLimited],
        ['重放拒绝次数', s.replayRejected],
        ['签名失败次数', s.signatureRejected],
        ['封禁命中请求', s.bannedRequests],
        ['当前活跃封禁', s.activeBans],
        ['被跟踪 IP 数', s.trackedIps]
      ].forEach(function (item) {
        var box = el('div', 'stat');
        box.appendChild(el('div', 'label', item[0]));
        box.appendChild(el('div', 'value', String(item[1])));
        stats.appendChild(box);
      });
      body.appendChild(stats);

      body.appendChild(el('div', 'divider'));
      body.appendChild(el('div', 'hint', '当前阈值'));
      body.appendChild(jsonBlock(limits));

      body.appendChild(el('div', 'divider'));
      body.appendChild(el('div', 'hint', '已生效的防线'));
      var defs = el('div', 'row tight');
      (res.data.defenses || []).forEach(function (d) { defs.appendChild(badge(d, 'info')); });
      body.appendChild(defs);

      body.appendChild(el('div', 'divider'));
      body.appendChild(el('div', 'hint', '按攻击分排序的 IP（点击行可解封）'));
      body.appendChild(table([
        { title: 'IP', key: 'ip' },
        { title: '攻击分', key: 'strikes' },
        { title: '蜜罐命中', key: 'honeypotHits' },
        { title: 'WAF 命中', key: 'wafHits' },
        { title: '登录失败', key: 'loginFails' },
        { title: '状态', render: function (r) { return r.banned ? badge('已封禁至 ' + U.fmtTime(r.bannedUntil), 'bad') : badge('监控中', 'info'); } },
        { title: '最近原因', key: 'lastReason' },
        { title: '事件轨迹', render: function (r) { return (r.events || []).slice(-3).join(' ｜ '); } }
      ], s.offsets || [], function (row) {
        if (!row.banned) { U.toast('该 IP 未被封禁', 'warn'); return; }
        API.post('/api/admin/security/unban', { ip: row.ip })
          .then(function (res2) { U.toast(res2.data.message, 'ok'); refreshCurrentTab(); })
          .catch(function (err) { U.toast(err.message, 'bad'); });
      }));
    }).catch(function (err) { body.textContent = '加载失败：' + err.message; });
  }

  //管理端 AI
  //追加一条对话
  function aiAdd(role, text) {
    var log = document.getElementById('ai-log');
    var wrap = el('div', 'msg ' + role);
    wrap.appendChild(el('div', 'msg-head', role === 'user' ? '管理员' : '安全助手'));
    wrap.appendChild(el('div', 'msg-body', text));
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
    return wrap;
  }

  //发送只读分析问题
  function aiAsk() {
    var input = document.getElementById('ai-input');
    var question = input.value.trim();
    if (!question) return;

    aiAdd('user', question);
    input.value = '';
    var pending = aiAdd('agent', '正在以只读方式分析…');
    var button = document.getElementById('btn-ai-send');
    button.disabled = true;

    API.post('/api/agent/chat', { message: question }).then(function (res) {
      pending.remove();
      var wrap = aiAdd('agent', res.data.reply || '（无回复）');
      if (res.data.result) wrap.appendChild(el('pre', 'code', U.json(res.data.result)));
      if (res.data.notes && res.data.notes.length) wrap.appendChild(el('div', 'hint', res.data.notes.join(' / ')));
    }).catch(function (err) {
      pending.remove();
      aiAdd('system', '请求失败：' + err.message);
    }).then(function () { button.disabled = false; });
  }

  //事件
  //刷新当前标签
  function refreshCurrentTab() { switchTab(state.tab); }

  //绑定页面事件
  function bind() {
    document.getElementById('btn-login').addEventListener('click', login);
    document.getElementById('login-pass').addEventListener('keydown', function (e) { if (e.key === 'Enter') login(); });
    document.getElementById('btn-logout').addEventListener('click', function () {
      API.post('/api/auth/logout', {}).catch(function () { /* 忽略 */ }).then(function () {
        showLogin();
        U.toast('已退出登录', 'ok');
      });
    });
    document.getElementById('btn-ai-send').addEventListener('click', aiAsk);
    document.getElementById('ai-input').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) aiAsk();
    });

    //改写账号标签文案
    var usersTab = document.querySelector('#admin-tabs .tab[data-tab="users"]');
    if (usersTab) usersTab.textContent = '账号管理';

    document.getElementById('admin-tabs').addEventListener('click', function (e) {
      var tab = e.target.closest('.tab');
      if (tab) switchTab(tab.getAttribute('data-tab'));
    });

    window.addEventListener('session-expired', function () {
      U.toast('登录状态已失效，请重新登录', 'warn');
      showLogin();
    });
  }

  bind();
  boot();
})();
