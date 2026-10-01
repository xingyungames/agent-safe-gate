/*
 * admin.js —— 管理后台。
 * 渲染原则同上：一律 textContent，不拼 HTML；所有写操作都带请求签名。
 */
(function () {
  'use strict';

  var el = U.el;
  var state = {
    tab: 'overview',
    pendingToken: null,   // 邮件链接里的签名令牌（仅用于"一次性批准"）
    pendingTaskId: null,
    currentUser: null
  };

  // ------------------------------------------------------------------ 启动与登录
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
        if (res.data.role !== 'admin') {
          API.clear();
          U.toast('该账号不是管理员，无权访问后台', 'bad');
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

  function showLogin() {
    document.getElementById('login-view').hidden = false;
    document.getElementById('admin-view').hidden = true;
    document.getElementById('userbox').hidden = true;
  }

  function enterAdmin() {
    document.getElementById('login-view').hidden = true;
    document.getElementById('admin-view').hidden = false;
    document.getElementById('userbox').hidden = false;
    document.getElementById('whoami').textContent = state.currentUser.displayName + '（' + state.currentUser.userName + '）';
    switchTab(state.pendingTaskId ? 'pending' : 'overview');
  }

  function login() {
    var userName = document.getElementById('login-user').value.trim();
    var password = document.getElementById('login-pass').value;
    if (!userName || !password) { U.toast('请输入用户名与口令', 'warn'); return; }

    API.public('/api/auth/login', { userName: userName, password: password }).then(function (res) {
      if (res.data.user.role !== 'admin') {
        U.toast('账号 ' + res.data.user.userName + ' 不是管理员，已拒绝进入后台', 'bad');
        return;
      }
      API.setSession(res.data);
      document.getElementById('login-pass').value = '';
      state.currentUser = res.data.user;
      U.toast('管理员登录成功', 'ok');
      enterAdmin();
    }).catch(function (err) { U.toast(err.message, 'bad'); });
  }

  // ------------------------------------------------------------------ 通用渲染件
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

  function badge(text, cls) { return el('span', 'badge ' + (cls || 'info'), text); }

  function section(title, node) {
    var wrap = el('div', null);
    wrap.appendChild(el('div', 'hint', title));
    wrap.appendChild(node);
    wrap.appendChild(el('div', 'divider'));
    return wrap;
  }

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

  function jsonBlock(value) { return el('pre', 'code', typeof value === 'string' ? value : U.json(value)); }

  // ------------------------------------------------------------------ 标签切换
  function switchTab(name) {
    state.tab = name;
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
      case 'security': loadSecurity(body); break;
      default: body.textContent = '未知标签';
    }
  }

  // ------------------------------------------------------------------ 总览
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

  // ------------------------------------------------------------------ 待审批 / 全部任务
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

      // 邮件审批链接进入的任务：显示一次性令牌校验面板
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

  // ------------------------------------------------------------------ 审批详情
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

  // ------------------------------------------------------------------ 版本与回退
  function loadVersions(body) {
    var row = el('div', 'row');
    var userInput = el('input', 'grow');
    userInput.type = 'text';
    userInput.placeholder = '按用户 ID 过滤（留空看全部）';
    row.appendChild(userInput);
    var btn = el('button', 'primary small', '查询');
    row.appendChild(btn);
    body.appendChild(row);
    body.appendChild(el('div', 'hint', '回退基于执行前保存的全量快照与反向操作数据；回退操作本身同样会留快照，可再次回退（撤销误回退）。'));
    body.appendChild(el('div', 'divider'));

    var host = el('div', null, '加载中…');
    body.appendChild(host);

    function run() {
      host.textContent = '加载中…';
      API.get('/api/admin/versions?limit=100&userId=' + encodeURIComponent(userInput.value.trim())).then(function (res) {
        U.clear(host);
        var list = res.data || [];
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

          if (!v.reverted) {
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
    run();
  }

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

  // ------------------------------------------------------------------ 用户管理
  function loadUsers(body) {
    API.get('/api/admin/users').then(function (res) {
      U.clear(body);

      var createRow = el('div', 'row');
      var name = el('input'); name.type = 'text'; name.placeholder = '用户名'; name.maxLength = 32;
      var display = el('input'); display.type = 'text'; display.placeholder = '显示名'; display.maxLength = 32;
      var pass = el('input'); pass.type = 'password'; pass.placeholder = '初始口令（≥10 位，三类字符）'; pass.maxLength = 128;
      var email = el('input'); email.type = 'email'; email.placeholder = '邮箱（可选）'; email.maxLength = 128;
      var role = el('select');
      [['user', '用户'], ['staff', '员工'], ['admin', '管理员']].forEach(function (pair) {
        var opt = el('option', null, pair[1]); opt.value = pair[0]; role.appendChild(opt);
      });
      var createBtn = el('button', 'primary small', '创建账号');

      [name, display, pass, email, role, createBtn].forEach(function (n) { createRow.appendChild(n); });
      body.appendChild(el('div', 'hint', '创建账号（口令强度：至少 10 位，且包含大写字母 / 小写字母 / 数字 / 符号中的三类）'));
      body.appendChild(createRow);
      body.appendChild(el('div', 'divider'));

      createBtn.addEventListener('click', function () {
        createBtn.disabled = true;
        API.post('/api/admin/users', {
          userName: name.value.trim(),
          displayName: display.value.trim(),
          password: pass.value,
          role: role.value,
          email: email.value.trim()
        }).then(function (r) { U.toast(r.data.message, 'ok'); refreshCurrentTab(); })
          .catch(function (e) { createBtn.disabled = false; U.toast(e.message, 'bad'); });
      });

      body.appendChild(table([
        { title: '用户名', key: 'userName' },
        { title: '显示名', key: 'displayName' },
        { title: '角色', render: function (r) { return badge(r.roleLabel, r.role === 'admin' ? 'bad' : r.role === 'staff' ? 'info' : 'ok'); } },
        { title: '状态', render: function (r) { return r.disabled ? badge('已禁用', 'bad') : r.locked ? badge('锁定中', 'warn') : badge('正常', 'ok'); } },
        { title: '邮箱', key: 'emailMasked' },
        { title: '最后登录', render: function (r) { return U.fmtTime(r.lastLoginAt) + (r.lastLoginIp ? '（' + r.lastLoginIp + '）' : ''); } },
        { title: '今日 AI 调用', key: 'aiCallsToday' },
        { title: '操作', render: function (r) { return userActions(r); } }
      ], res.data || []));
    }).catch(function (err) { body.textContent = '加载失败：' + err.message; });
  }

  function userActions(user) {
    var wrap = el('div', 'row tight');

    var roleSelect = el('select');
    [['user', '用户'], ['staff', '员工'], ['admin', '管理员']].forEach(function (pair) {
      var opt = el('option', null, pair[1]);
      opt.value = pair[0];
      if (pair[0] === user.role) opt.selected = true;
      roleSelect.appendChild(opt);
    });
    roleSelect.addEventListener('click', function (e) { e.stopPropagation(); });
    var roleBtn = el('button', 'small', '改角色');
    roleBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      API.post('/api/admin/users/' + encodeURIComponent(user.id) + '/role', { role: roleSelect.value })
        .then(function (r) { U.toast(r.data.message, 'ok'); refreshCurrentTab(); })
        .catch(function (err) { U.toast(err.message, 'bad'); });
    });

    var toggleBtn = el('button', 'small ' + (user.disabled ? 'ok' : 'danger'), user.disabled ? '启用' : '禁用');
    toggleBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      API.post('/api/admin/users/' + encodeURIComponent(user.id) + '/status', { disabled: !user.disabled })
        .then(function (r) { U.toast(r.data.message, 'ok'); refreshCurrentTab(); })
        .catch(function (err) { U.toast(err.message, 'bad'); });
    });

    var resetBtn = el('button', 'small ghost', '重置口令');
    resetBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      var box = el('div', null);
      var input = el('input');
      input.type = 'password';
      input.maxLength = 128;
      input.placeholder = '新口令（≥10 位，三类字符）';
      box.appendChild(el('div', 'hint', '重置后该账号所有令牌立即失效。'));
      box.appendChild(input);

      var footer = el('div', 'row tight');
      var ok = el('button', 'primary', '确认重置');
      ok.addEventListener('click', function () {
        API.post('/api/admin/users/' + encodeURIComponent(user.id) + '/reset-password', { newPassword: input.value })
          .then(function (r) { U.toast(r.data.message, 'ok'); U.clear(document.getElementById('modal-host')); })
          .catch(function (err) { U.toast(err.message, 'bad'); });
      });
      footer.appendChild(ok);
      openModal('重置口令 · ' + user.userName, box, footer);
    });

    wrap.appendChild(roleSelect);
    wrap.appendChild(roleBtn);
    wrap.appendChild(toggleBtn);
    wrap.appendChild(resetBtn);
    return wrap;
  }

  // ------------------------------------------------------------------ 审计日志
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
          { title: '详情', key: 'detail' },
          { title: '哈希', key: 'hash' }
        ], res.data || []));
      }).catch(function (err) { host.textContent = '加载失败：' + err.message; });
    }

    verify.addEventListener('click', function () {
      API.get('/api/admin/audit/verify').then(function (res) {
        U.clear(chainHost);
        chainHost.appendChild(el('div', 'banner ' + (res.data.ok ? 'ok' : 'bad'), res.data.message));
      }).catch(function (err) { U.toast(err.message, 'bad'); });
    });

    btn.addEventListener('click', run);
    run();
  }

  // ------------------------------------------------------------------ 出站通知
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

  // ------------------------------------------------------------------ 业务数据
  function loadDatasets(body) {
    var select = el('select');
    var host = el('div', null, '加载中…');
    var row = el('div', 'row');
    row.appendChild(el('span', 'hint', '数据集：'));
    row.appendChild(select);
    body.appendChild(row);
    body.appendChild(el('div', 'divider'));
    body.appendChild(host);

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

  // ------------------------------------------------------------------ 安全态势
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

  // ------------------------------------------------------------------ 管理端 AI
  function aiAdd(role, text) {
    var log = document.getElementById('ai-log');
    var wrap = el('div', 'msg ' + role);
    wrap.appendChild(el('div', 'msg-head', role === 'user' ? '管理员' : '安全助手'));
    wrap.appendChild(el('div', 'msg-body', text));
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
    return wrap;
  }

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

  // ------------------------------------------------------------------ 事件
  function refreshCurrentTab() { switchTab(state.tab); }

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
