/*
 * app.js —— 业务工作台（用户 / 员工）。
 * 渲染原则：所有来自服务端或用户输入的文本一律通过 textContent 写入 DOM，绝不拼 HTML 字符串。
 */
(function () {
  'use strict';

  var el = U.el;
  var state = {
    sessionId: null,
    health: null,
    tools: null,
    currentFile: null
  };

  // ------------------------------------------------------------------ 启动
  function boot() {
    API.publicGet('/api/public/health').then(function (res) {
      state.health = res.data;
      var aiStatus = res.data.aiStatus || (res.data.aiConfigured ? 'online' : 'off');

      var banner = document.getElementById('env-banner');
      U.clear(banner);
      banner.appendChild(el('div', null, '服务已就绪。' + res.data.mode));
      banner.appendChild(el('div', 'hint',
        'AI 状态：' + (aiStatus === 'online' ? '在线' : aiStatus === 'degraded' ? '降级（调用失败）' : '未配置')
        + (aiStatus === 'degraded' && res.data.aiLastError ? '　原因：' + res.data.aiLastError : '')
        + '；自助注册：' + (res.data.selfRegistration ? '开启（仅用户角色）' : '已关闭')));

      var mode = document.getElementById('ai-mode');
      if (aiStatus === 'online') {
        mode.textContent = 'AI 在线';
        mode.className = 'badge ok';
      } else if (aiStatus === 'degraded') {
        mode.textContent = 'AI 降级';
        mode.className = 'badge warn';
      } else {
        mode.textContent = '本地模式';
        mode.className = 'badge warn';
      }

      var hint = document.getElementById('mode-hint');
      hint.textContent = aiStatus === 'online'
        ? 'AI 负责理解意图；能否执行由规则引擎 + AI 安全审核 + 人工审批决定。'
        : 'AI 不可用：使用本地指令解析，变更类操作全部转人工审批（防线不降级）。';
    }).catch(function (err) {
      document.getElementById('env-banner').textContent = '无法连接服务：' + err.message;
    });

    if (API.isLoggedIn()) {
      API.get('/api/auth/me').then(function () { enterConsole(); }).catch(function () { showLogin(); });
    } else {
      showLogin();
    }
  }

  function showLogin() {
    document.getElementById('login-view').hidden = false;
    document.getElementById('console-view').hidden = true;
    document.getElementById('userbox').hidden = true;
    API.clear();
  }

  function enterConsole() {
    var user = API.session().user;
    document.getElementById('login-view').hidden = true;
    document.getElementById('console-view').hidden = false;
    document.getElementById('userbox').hidden = false;
    document.getElementById('whoami').textContent = user.displayName + '（' + user.userName + '）';
    document.getElementById('rolebadge').textContent = user.roleLabel;
    document.getElementById('rolebadge').className = 'badge ' + (user.role === 'admin' ? 'bad' : user.role === 'staff' ? 'info' : 'ok');

    renderChips();
    switchTab('tasks');
    if (!document.querySelector('.msg')) {
      addMessage('system', '你好，' + user.displayName + '。当前角色：' + user.roleLabel +
        '。可以直接说出你要做的事，例如“列出我的文件”“查询客户 等级=黄金”。' +
        '涉及删除、修改数据、发送通知、覆盖文件的请求会先提交管理员审批。');
    }
  }

  // ------------------------------------------------------------------ 登录 / 注册
  function login() {
    var userName = document.getElementById('login-user').value.trim();
    var password = document.getElementById('login-pass').value;
    if (!userName || !password) { U.toast('请输入用户名与口令', 'warn'); return; }

    API.public('/api/auth/login', { userName: userName, password: password }).then(function (res) {
      API.setSession(res.data);
      document.getElementById('login-pass').value = '';
      U.toast('登录成功', 'ok');
      enterConsole();
    }).catch(function (err) {
      U.toast(err.message, 'bad');
    });
  }

  function register() {
    var payload = {
      userName: document.getElementById('reg-user').value.trim(),
      displayName: document.getElementById('reg-display').value.trim(),
      password: document.getElementById('reg-pass').value,
      email: document.getElementById('reg-email').value.trim()
    };

    API.public('/api/auth/register', payload).then(function (res) {
      U.toast(res.data.message, 'ok');
      document.getElementById('login-user').value = payload.userName;
      document.getElementById('register-card').hidden = true;
    }).catch(function (err) {
      U.toast(err.message, 'bad');
    });
  }

  // ------------------------------------------------------------------ 对话
  function renderChips() {
    var host = U.clear(document.getElementById('quick-chips'));
    var user = API.session().user || {};
    var samples = [
      '帮我看一下工作区里有哪些文件，然后总结一下内容',
      '读一下日报，告诉我写了什么',
      '把今天的进展写进周报.txt',
      '我的可用区域与权限',
      '我的通知',
      '有多少客户',
      '示例物流现在什么情况',
      '把示例物流的负责人改成 lisi',
      '把示例智造集团标记为重要',
      '把 日报.txt 改名为 工作日志.txt'
    ];

    if (user.role === 'staff' || user.role === 'admin') {
      samples.unshift('查一下等级为重要的客户有哪些');
      samples.push('把「设备数据看板」这条需求标记为已报价');
    }
    if (user.role === 'admin') {
      samples.unshift('目前帮我查一下有多少用户');
      samples.push('系统里有哪些员工账号');
    }

    samples.forEach(function (text) {
      var chip = el('span', 'chip', text);
      chip.addEventListener('click', function () {
        document.getElementById('chat-input').value = text;
      });
      host.appendChild(chip);
    });
  }

  function addMessage(kind, text) {
    var log = document.getElementById('chat-log');
    var wrap = el('div', 'msg ' + kind);
    wrap.appendChild(el('div', 'msg-head', kind === 'user' ? '我' : kind === 'agent' ? '智能体' : '系统'));
    wrap.appendChild(el('div', 'msg-body', text));
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
    return wrap;
  }

  function addActionCard(container, data) {
    var card = el('div', 'action-card');

    function kv(key, value, cls) {
      var row = el('div', 'kv');
      row.appendChild(el('span', 'k', key));
      var v = el('span', 'v', value === undefined || value === null || value === '' ? '-' : value);
      if (cls) v.className = 'v ' + cls;
      row.appendChild(v);
      card.appendChild(row);
      return v;
    }

    kv('状态', data.statusLabel + '（' + data.status + '）');
    if (data.intent) kv('意图摘要', data.intent);
    if (data.tool) kv('工具', data.tool);
    kv('风险等级', data.riskLabel || '-');
    if (data.taskId) kv('任务号', data.taskId);
    if (data.reviewVerdict) kv('结构审核', data.reviewVerdict);
    if (data.actionSummary) kv('动作摘要', data.actionSummary);

    // 多步执行轨迹：像代码助手那样展示"先搜 → 再读 → 再写"
    if (data.steps && data.steps.length) {
      var chain = el('div', 'steps');
      chain.appendChild(el('div', 'steps-title', '执行步骤（' + data.steps.length + ' 步）'));
      data.steps.forEach(function (step) {
        var item = el('div', 'step');
        item.appendChild(el('span', 'badge ' + (step.status === 'OK' ? 'ok' : 'warn'), String(step.index)));
        item.appendChild(el('span', 'step-tool', step.title + (step.tool ? '（' + step.tool + '）' : '')));
        item.appendChild(el('span', 'step-summary', step.summary || ''));
        if (step.elapsedMs) item.appendChild(el('span', 'step-time', step.elapsedMs + 'ms'));
        chain.appendChild(item);

        if (step.result) {
          var details = el('details', 'step-result');
          details.appendChild(el('summary', null, '查看这一步的真实结果'));
          details.appendChild(el('pre', 'code', U.json(step.result)));
          chain.appendChild(details);
        }
      });
      card.appendChild(chain);
    }

    // 系统做的"口语 → 规范值"翻译（例如「重要」→ 铂金）
    if (data.translations && data.translations.length) {
      kv('参数映射', data.translations.join('；'), 'diff-add');
    }

    if (data.injectionFlags && data.injectionFlags.length) {
      kv('注入特征', data.injectionFlags.join('、'), 'diff-del');
    }

    if (data.notes && data.notes.length) kv('说明', data.notes.join(' / '));

    if (data.stepLimitReached) kv('提示', '已达到单次最多步数；如需继续，请再发一条更具体的请求。');

    if (data.result) {
      var pre = el('pre', 'code', U.json(data.result));
      card.appendChild(pre);
    }

    container.appendChild(card);
  }

  function send() {
    var input = document.getElementById('chat-input');
    var message = input.value.trim();
    if (!message) { U.toast('请输入内容', 'warn'); return; }

    addMessage('user', message);
    input.value = '';
    var pending = addMessage('agent', '正在解析意图并经过规则引擎与安全审核…');
    document.getElementById('btn-send').disabled = true;

    API.post('/api/agent/chat', { message: message, sessionId: state.sessionId }).then(function (res) {
      state.sessionId = res.data.sessionId;
      pending.remove();
      var wrap = addMessage('agent', res.data.reply || '（无回复）');
      addActionCard(wrap, res.data);
      if (res.data.status === 'PENDING_APPROVAL') {
        U.toast('已提交人工审批：' + res.data.taskId, 'warn');
        switchTab('tasks');
      }
    }).catch(function (err) {
      pending.remove();
      addMessage('system', '请求失败：' + err.message + (err.code ? '（' + err.code + '）' : ''));
    }).then(function () {
      document.getElementById('btn-send').disabled = false;
    });
  }

  // ------------------------------------------------------------------ 侧栏标签
  function switchTab(name) {
    var tabs = document.querySelectorAll('#side-tabs .tab');
    Array.prototype.forEach.call(tabs, function (t) {
      t.classList.toggle('active', t.getAttribute('data-tab') === name);
    });

    var body = document.getElementById('side-body');
    body.textContent = '加载中…';

    if (name === 'tasks') loadTasks(body);
    else if (name === 'files') loadFiles(body, 'private', '');
    else if (name === 'notices') loadNotices(body);
    else if (name === 'versions') loadVersions(body);
    else if (name === 'perm') loadPermissions(body);
  }

  function loadTasks(body) {
    API.get('/api/agent/tasks').then(function (res) {
      U.clear(body);
      var list = res.data || [];
      if (!list.length) { body.appendChild(el('div', 'empty', '还没有任务记录')); return; }

      var wrap = el('div', 'list');
      list.forEach(function (task) {
        var item = el('div', 'list-item');
        item.appendChild(el('div', 'title', task.intent || task.tool));
        var meta = el('div', 'meta');
        meta.appendChild(el('span', 'badge ' + U.statusClass(task.status), task.statusLabel));
        meta.appendChild(document.createTextNode('  ' + U.fmtTime(task.createdAt) + '  风险 ' + task.riskLabel + '  ' + task.id));
        item.appendChild(meta);

        // 待审批的任务可以自己撤回（撤回只取消排队，不执行任何动作）
        if (task.status === 'PENDING_APPROVAL') {
          var actions = el('div', 'row tight');
          var cancel = el('button', 'small ghost', '撤回申请');
          cancel.addEventListener('click', function (e) {
            e.stopPropagation();
            cancel.disabled = true;
            API.post('/api/biz/action', {
              tool: 'cancel_my_task',
              args: { task_id: task.id },
              reason: '用户在任务列表里撤回'
            }).then(function (res) {
              U.toast((res.data && res.data.reply) || '已撤回申请', 'ok');
              switchTab('tasks');
            }).catch(function (err) {
              cancel.disabled = false;
              U.toast(err.message, 'bad');
            });
          });
          actions.appendChild(cancel);
          actions.appendChild(el('span', 'hint', '撤回后不会执行任何动作'));
          item.appendChild(actions);
        }

        item.addEventListener('click', function () { showTaskDetail(task.id); });
        wrap.appendChild(item);
      });
      body.appendChild(wrap);
    }).catch(function (err) { body.textContent = '加载失败：' + err.message; });
  }

  function showTaskDetail(id) {
    API.get('/api/agent/tasks/' + encodeURIComponent(id)).then(function (res) {
      var task = res.data;
      var body = el('div', null);

      function section(title, value) {
        body.appendChild(el('div', 'divider'));
        body.appendChild(el('div', 'hint', title));
        body.appendChild(el('pre', 'code', typeof value === 'string' ? value : U.json(value)));
      }

      section('任务概况', {
        任务号: task.id,
        状态: task.statusLabel,
        状态说明: task.statusReason,
        意图: task.intent,
        工具: task.tool,
        风险: task.riskLabel,
        创建时间: U.fmtTime(task.createdAt),
        审批人: task.decidedBy || '-',
        审批意见: task.decisionReason || '-',
        执行时间: U.fmtTime(task.executedAt)
      });

      section('规范化动作（执行端只认这一份）', task.action);
      if (task.ruleReasons && task.ruleReasons.length) section('规则引擎结论', task.ruleReasons.join('；'));
      section('AI 安全审核', { verdict: task.reviewVerdict, reason: task.reviewReason });
      if (task.injectionFlags && task.injectionFlags.length) section('输入侧注入特征', task.injectionFlags.join('、'));
      if (task.result) section('执行结果', task.result);

      openModal('任务详情 ' + task.id, body);
    }).catch(function (err) { U.toast(err.message, 'bad'); });
  }

  function loadFiles(body, scope, path) {
    API.get('/api/agent/files?scope=' + encodeURIComponent(scope) + '&path=' + encodeURIComponent(path))
      .then(function (res) {
        U.clear(body);

        var head = el('div', 'row tight');
        head.appendChild(el('span', 'hint', (scope === 'share' ? '共享区' : '个人区') + ' / ' + (path || '')));
        if (path) {
          var up = el('button', 'small ghost', '返回上级');
          up.addEventListener('click', function () {
            var parent = path.indexOf('/') >= 0 ? path.substring(0, path.lastIndexOf('/')) : '';
            loadFiles(body, scope, parent);
          });
          head.appendChild(up);
        }
        var toggle = el('button', 'small ghost', scope === 'share' ? '看个人区' : '看共享区');
        toggle.addEventListener('click', function () { loadFiles(body, scope === 'share' ? 'private' : 'share', ''); });
        head.appendChild(toggle);
        body.appendChild(head);

        var entries = (res.data.data && res.data.data.entries) || [];
        if (!entries.length) { body.appendChild(el('div', 'empty', '目录为空')); return; }

        var list = el('div', 'list');
        entries.forEach(function (entry) {
          var item = el('div', 'list-item');
          item.appendChild(el('div', 'title', (entry.type === 'dir' ? '[目录] ' : '[文件] ') + entry.name));
          item.appendChild(el('div', 'meta', entry.type === 'file' ? (entry.size + ' 字节 · ' + U.fmtTime(entry.modified)) : U.fmtTime(entry.modified)));
          item.addEventListener('click', function () {
            if (entry.type === 'dir') {
              loadFiles(body, scope, path ? path + '/' + entry.name : entry.name);
            } else {
              viewFile(scope, path ? path + '/' + entry.name : entry.name);
            }
          });
          list.appendChild(item);
        });
        body.appendChild(list);
      }).catch(function (err) {
        U.clear(body);
        body.appendChild(el('div', 'banner bad', '读取失败：' + err.message));
      });
  }

  function viewFile(scope, path) {
    API.get('/api/agent/files/content?scope=' + encodeURIComponent(scope) + '&path=' + encodeURIComponent(path))
      .then(function (res) {
        var body = el('div', null);
        body.appendChild(el('div', 'hint', '路径：' + scope + '/' + res.data.data.path +
          '　大小：' + res.data.data.size + ' 字节　SHA-256：' + res.data.data.sha256));
        body.appendChild(el('pre', 'code', res.data.data.content));
        openModal('文件内容：' + res.data.data.path, body);
      }).catch(function (err) { U.toast(err.message, 'bad'); });
  }

  function loadNotices(body) {
    API.get('/api/agent/notices').then(function (res) {
      U.clear(body);
      var list = res.data || [];
      if (!list.length) { body.appendChild(el('div', 'empty', '还没有通知')); return; }

      list.forEach(function (n) {
        var card = el('div', 'list-item');
        card.appendChild(el('div', 'title', n.subject));
        var meta = el('div', 'meta');
        meta.appendChild(el('span', 'badge ' + (n.status === 'SENT' ? 'ok' : n.status === 'RECALLED' ? 'bad' : 'info'), n.status));
        meta.appendChild(document.createTextNode('  ' + U.fmtTime(n.createdAt) + '  任务 ' + (n.taskId || '-')));
        card.appendChild(meta);
        card.appendChild(el('pre', 'code', n.body));
        body.appendChild(card);
      });
    }).catch(function (err) { body.textContent = '加载失败：' + err.message; });
  }

  function loadVersions(body) {
    API.get('/api/agent/versions').then(function (res) {
      U.clear(body);
      var list = res.data || [];
      if (!list.length) { body.appendChild(el('div', 'empty', '还没有版本记录（写操作执行后会自动留快照）')); return; }

      list.forEach(function (v) {
        var card = el('div', 'list-item');
        card.appendChild(el('div', 'title', v.operation + ' ' + v.target));
        var meta = el('div', 'meta');
        meta.appendChild(el('span', 'badge ' + (v.reverted ? 'bad' : 'ok'), v.reverted ? ('已回退 by ' + (v.revertedBy || '')) : '可回退'));
        meta.appendChild(document.createTextNode('  +' + v.addedLines + ' / -' + v.removedLines + '  ' + U.fmtTime(v.createdAt)));
        card.appendChild(meta);

        var pre = el('pre', 'code');
        (v.diff || '').split('\n').slice(0, 120).forEach(function (line) {
          pre.appendChild(el('span', line.charAt(0) === '+' ? 'diff-add' : line.charAt(0) === '-' ? 'diff-del' : null, line + '\n'));
        });
        card.appendChild(pre);
        body.appendChild(card);
      });
    }).catch(function (err) { body.textContent = '加载失败：' + err.message; });
  }

  function loadPermissions(body) {
    API.get('/api/agent/tools').then(function (res) {
      U.clear(body);
      body.appendChild(el('div', 'hint', '当前角色：' + res.data.roleLabel));

      var perms = el('div', 'row tight');
      (res.data.permissions || []).forEach(function (p) { perms.appendChild(el('span', 'badge info', p.label)); });
      body.appendChild(perms);
      body.appendChild(el('div', 'divider'));

      (res.data.tools || []).forEach(function (t) {
        var card = el('div', 'list-item');
        var title = el('div', 'title');
        title.appendChild(el('span', null, t.title + '（' + t.name + '）'));
        title.appendChild(document.createTextNode('  '));
        title.appendChild(el('span', 'badge ' + U.riskClass(t.risk), '风险 ' + t.riskLabel));
        if (t.requiresApproval) title.appendChild(el('span', 'badge warn', ' 需审批'));
        card.appendChild(title);
        card.appendChild(el('div', 'meta', t.description));
        if (t.required && t.required.length) card.appendChild(el('div', 'meta', '必填参数：' + t.required.join('、')));
        body.appendChild(card);
      });
    }).catch(function (err) { body.textContent = '加载失败：' + err.message; });
  }

  // ------------------------------------------------------------------ 弹层
  function openModal(title, contentNode) {
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
    mask.appendChild(modal);
    mask.addEventListener('click', function (e) { if (e.target === mask) U.clear(host); });
    host.appendChild(mask);
  }

  // ------------------------------------------------------------------ 事件绑定
  function bind() {
    document.getElementById('btn-login').addEventListener('click', login);
    document.getElementById('login-pass').addEventListener('keydown', function (e) { if (e.key === 'Enter') login(); });
    document.getElementById('btn-register').addEventListener('click', register);
    document.getElementById('btn-show-register').addEventListener('click', function () {
      document.getElementById('register-card').hidden = false;
    });
    document.getElementById('btn-hide-register').addEventListener('click', function () {
      document.getElementById('register-card').hidden = true;
    });
    document.getElementById('btn-send').addEventListener('click', send);
    document.getElementById('chat-input').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) send();
    });
    document.getElementById('btn-reset-session').addEventListener('click', function () {
      API.post('/api/agent/session/reset', {}).then(function () {
        state.sessionId = null;
        U.clear(document.getElementById('chat-log'));
        addMessage('system', '对话上下文已清空（服务端记忆按用户隔离，不会与其他账号共享）。');
      }).catch(function (err) { U.toast(err.message, 'bad'); });
    });
    document.getElementById('btn-logout').addEventListener('click', function () {
      API.post('/api/auth/logout', {}).catch(function () { /* 忽略 */ }).then(function () {
        state.sessionId = null;
        U.clear(document.getElementById('chat-log'));
        showLogin();
        U.toast('已退出登录', 'ok');
      });
    });
    document.getElementById('btn-help').addEventListener('click', showHelp);

    document.getElementById('side-tabs').addEventListener('click', function (e) {
      var tab = e.target.closest('.tab');
      if (tab) switchTab(tab.getAttribute('data-tab'));
    });

    window.addEventListener('session-expired', function () {
      U.toast('登录状态已失效，请重新登录', 'warn');
      showLogin();
    });
  }

  function showHelp() {
    API.get('/api/agent/help').then(function (res) {
      var body = el('div', null);
      body.appendChild(el('div', 'hint', '本平台把“AI 的智能”和“系统的权限”彻底分开：AI 只负责理解你说的意图，能否执行由服务端规则引擎、AI 安全审核与人工审批共同决定。'));
      body.appendChild(el('div', 'divider'));
      body.appendChild(el('div', 'hint', res.data.localInstructions));
      body.appendChild(el('div', 'divider'));
      res.data.examples.forEach(function (sample) { body.appendChild(el('div', 'list-item', sample)); });
      openModal('使用说明', body);
    }).catch(function (err) { U.toast(err.message, 'bad'); });
  }

  bind();
  boot();
})();
