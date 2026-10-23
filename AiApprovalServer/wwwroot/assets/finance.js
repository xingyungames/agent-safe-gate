//财务工作台页面
(function () {
  'use strict';

  var el = U.el;
  var state = {
    currentUser: null,
    permissions: [],
    meta: null,
    rows: [],
    selected: null,
    filters: { status: '', type: '', department: '', keyword: '' },
    //AI 预审：recordId -> 评分视图；AI 选单候选
    scores: {},
    aiStats: null,
    aiAvailable: true,
    aiError: '',
    aiPicked: []
  };

  var FINANCE_ROLES = { finance: 1, admin: 1, superadmin: 1 };
  var ENUM_FIELDS = [
    { name: '部门', source: 'departments' },
    { name: '报销类型', source: 'types' },
    { name: '票据状态', source: 'invoiceStates' },
    { name: '城市', source: 'cities' },
    { name: '合规结果', source: 'complianceResults' },
    { name: '状态', source: 'statuses' }
  ];

  //通用工具

  //权限判断
  function has(permission) {
    return state.permissions.indexOf(permission) >= 0;
  }

  //空值显示为短横
  function elText(value) {
    if (value === undefined || value === null || value === '') return '-';
    return String(value);
  }

  //时间戳转时间文本
  function fmtMs(value) {
    if (value === undefined || value === null || value === '') return '-';
    var raw = String(value).trim();
    if (/^\d{11,}$/.test(raw)) {
      var d = new Date(Number(raw));
      if (!isNaN(d.getTime())) return U.fmtTime(d.toISOString());
    }
    return U.fmtTime(raw);
  }

  //生成状态徽标
  function badge(text, cls) { return el('span', 'badge ' + (cls || 'info'), text); }

  //AI 等级样式类（1-4，0=未评分）
  function lvlClass(level) {
    var n = Number(level) || 0;
    return (n >= 1 && n <= 4) ? 'lvl' + n : 'lvl0';
  }

  //AI 等级中文标签
  function lvlText(level) {
    switch (Number(level) || 0) {
      case 1: return '1 正常';
      case 2: return '2 注意';
      case 3: return '3 危险';
      case 4: return '4 严重';
      default: return '未评分';
    }
  }

  //取某单的 AI 评分
  function scoreOf(recordId) {
    return state.scores[recordId] || null;
  }

  var HTTPS_PREFIX = 'https://';

  //只放行 https 地址
  function safeHttpsUrl(value) {
    if (typeof value !== 'string') return '';
    var text = value.trim();
    if (text.length <= HTTPS_PREFIX.length) return '';
    if (text.toLowerCase().slice(0, HTTPS_PREFIX.length) !== HTTPS_PREFIX) return '';
    return text;
  }

  //单据状态配色
  function statusClass(status) {
    if (status === '已通过') return 'ok';
    if (status === '已驳回') return 'bad';
    if (status === '审核中') return 'info';
    if (status === '待审核') return 'warn';
    return 'muted';
  }

  //合规结果配色
  function complianceClass(result) {
    if (result === '通过') return 'ok';
    if (result === '风险') return 'warn';
    if (result === '驳回') return 'bad';
    return 'muted';
  }

  //票据状态配色
  function invoiceStateClass(state_) {
    if (state_ === '有票') return 'ok';
    if (state_ === '未提') return 'warn';
    if (state_ === '无票') return 'bad';
    return 'muted';
  }

  //生成下拉框
  function select(options, current, placeholder) {
    var node = el('select');
    if (placeholder) {
      var ph = el('option', null, placeholder);
      ph.value = '';
      node.appendChild(ph);
    }
    options.forEach(function (value) {
      var opt = el('option', null, value);
      opt.value = value;
      if (value === current) opt.selected = true;
      node.appendChild(opt);
    });
    return node;
  }

  //包装表单字段
  function field(labelText, control) {
    var wrap = el('label', 'field');
    wrap.appendChild(el('span', null, labelText));
    wrap.appendChild(control);
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
    return host;
  }

  //把错误转成提示文案
  function describeError(err) {
    var status = err && err.status;
    var message = (err && err.message) || '';
    if (status === 401) return '登录状态已失效，请重新登录';
    if (status === 403) return message ? ('无权操作：' + message) : '无权操作：服务端拒绝了该请求';
    if (status === 400) return message || '服务端拒绝了该请求';
    if (status === 429) return message || '操作过于频繁，请稍后再试';
    if (status && status >= 500) return message ? ('服务端不可用：' + message) : '服务暂时不可用，请稍后重试';
    return message || '请求失败';
  }

  //统一上报错误
  function reportError(err) {
    if (err && err.status === 401) {
      showLogin('登录状态已失效，请重新登录');
      return;
    }
    U.toast(describeError(err), 'bad');
  }

  //提示服务端返回消息
  function toastMessage(responseData) {
    var message = responseData && responseData.message;
    U.toast(message || '操作已完成', 'ok');
  }

  //启动与登录

  //页面启动
  function boot() {
    if (!API.isLoggedIn()) { showLogin(); return; }
    API.get('/api/auth/me').then(function (res) {
      var user = res.data || {};
      if (!FINANCE_ROLES[user.role]) { showDeny(user); return; }
      state.currentUser = user;
      state.permissions = (user.permissions || []).map(function (p) { return p.key; });
      enter();
    }).catch(function () { showLogin(); });
  }

  //切回登录页
  function showLogin(message) {
    document.getElementById('login-view').hidden = false;
    document.getElementById('deny-view').hidden = true;
    document.getElementById('finance-view').hidden = true;
    document.getElementById('userbox').hidden = true;
    if (message) document.getElementById('login-banner').textContent = message;
  }

  //显示无权限页
  function showDeny(user) {
    document.getElementById('login-view').hidden = true;
    document.getElementById('finance-view').hidden = true;
    document.getElementById('deny-view').hidden = false;
    document.getElementById('deny-detail').textContent =
      '当前账号 ' + elText(user.userName) + ' 的角色是「' + elText(user.roleLabel) + '」，无权访问财务工作台。';
  }

  //进入财务工作台
  function enter() {
    document.getElementById('login-view').hidden = true;
    document.getElementById('deny-view').hidden = true;
    document.getElementById('finance-view').hidden = false;
    document.getElementById('userbox').hidden = false;
    document.getElementById('whoami').textContent =
      elText(state.currentUser.displayName) + '（' + elText(state.currentUser.roleLabel) +
      '　工号 ' + elText(state.currentUser.employeeId) + '）';

    loadMeta();
    loadList();
    loadScores(false);
  }

  //提交登录
  function login() {
    var userName = document.getElementById('login-user').value.trim();
    var password = document.getElementById('login-pass').value;
    if (!userName || !password) { U.toast('请输入用户名与口令', 'warn'); return; }

    API.public('/api/auth/login', { userName: userName, password: password }).then(function (res) {
      var user = res.data.user;
      if (!FINANCE_ROLES[user.role]) {
        U.toast('账号 ' + user.userName + ' 的角色是「' + user.roleLabel + '」，无权进入财务工作台', 'bad');
        API.clear();
        showDeny(user);
        return;
      }
      API.setSession(res.data);
      document.getElementById('login-pass').value = '';
      state.currentUser = user;
      state.permissions = (user.permissions || []).map(function (p) { return p.key; });
      U.toast('登录成功', 'ok');
      enter();
    }).catch(function (err) { U.toast(describeError(err), 'bad'); });
  }

  //元数据

  //加载元数据
  function loadMeta() {
    API.get('/api/invoice/meta').then(function (res) {
      state.meta = res.data || {};
      fillSelect('filter-status', state.meta.statuses || []);
      fillSelect('filter-type', state.meta.types || []);
      fillSelect('filter-department', state.meta.departments || []);

      var rules = U.clear(document.getElementById('meta-rules'));
      (state.meta.rules || []).forEach(function (rule) { rules.appendChild(el('div', null, rule)); });

      var fields = U.clear(document.getElementById('meta-fields'));
      fields.appendChild(el('div', null, '财务可改字段：' + (state.meta.financeEditableFields || []).join('、')));
      fields.appendChild(el('div', null, '服务端盖章字段：' + (state.meta.serverStampedFields || []).join('、')));
      fields.appendChild(el('div', null, '只读字段：' + (state.meta.readonlyFields || []).join('、')));
      var range = state.meta.amountRange || {};
      fields.appendChild(el('div', null, '金额范围：' + elText(range.min) + ' 到 ' + elText(range.max)));

      renderFeishuTableLink();
    }).catch(function (err) { reportError(err); });
  }

  //渲染飞书原表链接
  function renderFeishuTableLink() {
    var link = document.getElementById('finance-feishu-table');
    var note = document.getElementById('finance-feishu-note');
    var meta = state.meta || {};
    var url = safeHttpsUrl(meta.tableUrl);

    if (url) {
      link.href = url;
      link.hidden = false;
      note.hidden = true;
      return;
    }

    link.removeAttribute('href');
    link.hidden = true;
    if (meta.feishuConfigured === false) {
      note.textContent = '服务端尚未配置飞书，暂时不能跳转到原表。';
      note.hidden = false;
    } else {
      note.hidden = true;
    }
  }

  //填充筛选下拉框
  function fillSelect(id, options) {
    var node = document.getElementById(id);
    var current = node.value;
    var label = node.getAttribute('data-placeholder') || '全部';
    while (node.firstChild) node.removeChild(node.firstChild);
    var first = el('option', null, label);
    first.value = '';
    node.appendChild(first);
    options.forEach(function (value) {
      var opt = el('option', null, value);
      opt.value = value;
      node.appendChild(opt);
    });
    node.value = current;
  }

  //列表

  //加载单据列表
  function loadList() {
    var host = document.getElementById('list-host');
    host.textContent = '加载中';
    API.get('/api/invoice/list?limit=200').then(function (res) {
      var data = res.data || {};
      state.rows = data.rows || [];
      document.getElementById('count-label').textContent =
        (data.scope === 'all' ? '全部单据' : '我的单据') + '　共 ' + state.rows.length + ' 条';
      renderList();
    }).catch(function (err) {
      host.textContent = '加载失败：' + describeError(err);
    });
  }

  //按条件过滤
  function filteredRows() {
    var f = state.filters;
    return state.rows.filter(function (row) {
      if (f.status && row.status !== f.status) return false;
      if (f.type && row.rtype !== f.type) return false;
      if (f.department && row.department !== f.department) return false;
      if (f.keyword) {
        var key = f.keyword.toLowerCase();
        var billNo = String(row.billNo || '').toLowerCase();
        var applicant = String(row.applicant || '').toLowerCase();
        if (billNo.indexOf(key) < 0 && applicant.indexOf(key) < 0) return false;
      }
      return true;
    });
  }

  //渲染单据表格
  function renderList() {
    var host = U.clear(document.getElementById('list-host'));
    var rows = filteredRows();
    if (!rows.length) {
      host.appendChild(el('div', 'empty', state.rows.length ? '没有符合筛选条件的单据' : '暂无报销单'));
      return;
    }

    var t = el('table');
    var thead = el('thead');
    var headRow = el('tr');
    ['单号', '申请人', '工号', '部门', '报销类型', '金额', '票据状态', '合规结果', '风险摘要', 'AI 预审', '状态', '审核人'].forEach(function (title) {
      headRow.appendChild(el('th', null, title));
    });
    thead.appendChild(headRow);
    t.appendChild(thead);

    var tbody = el('tbody');
    rows.forEach(function (row) {
      var tr = el('tr');
      tr.classList.add('clickable');
      tr.appendChild(el('td', null, elText(row.billNo)));
      tr.appendChild(el('td', null, elText(row.applicant)));
      tr.appendChild(el('td', null, elText(row.employeeId)));
      tr.appendChild(el('td', null, elText(row.department)));
      tr.appendChild(el('td', null, elText(row.rtype)));

      var amountCell = el('td');
      amountCell.textContent = row.amount === undefined || row.amount === null ? '-' : String(row.amount);
      tr.appendChild(amountCell);

      var invoiceCell = el('td');
      invoiceCell.appendChild(badge(elText(row.hasInvoice), invoiceStateClass(row.hasInvoice)));
      tr.appendChild(invoiceCell);

      var complianceCell = el('td');
      complianceCell.appendChild(badge(elText(row.compliance), complianceClass(row.compliance)));
      tr.appendChild(complianceCell);

      var riskCell = el('td');
      riskCell.textContent = elText(row.riskSummary);
      tr.appendChild(riskCell);

      //AI 预审等级（只读建议，不改变单据状态）
      var aiCell = el('td');
      var score = scoreOf(row.recordId);
      var lvl = score ? Number(score.finalLevel) || 0 : 0;
      aiCell.appendChild(badge(lvlText(lvl), lvlClass(lvl)));
      if (score && score.aiReason) {
        var why = el('div', 'hint');
        why.textContent = score.aiReason;
        aiCell.appendChild(why);
      }
      tr.appendChild(aiCell);
      if (lvl >= 1 && lvl <= 4) tr.classList.add('ailvl' + lvl);

      var statusCell = el('td');
      statusCell.appendChild(badge(elText(row.status), statusClass(row.status)));
      tr.appendChild(statusCell);

      tr.appendChild(el('td', null, elText(row.reviewer)));
      tr.addEventListener('click', function () { selectRow(row); });
      if (state.selected && state.selected.recordId === row.recordId) tr.classList.add('active');
      tbody.appendChild(tr);
    });
    t.appendChild(tbody);
    host.appendChild(t);
  }

  //选中一行
  function selectRow(row) {
    state.selected = row;
    renderList();
    renderDetail();
  }

  //详情与操作

  //渲染详情面板
  function renderDetail() {
    var host = U.clear(document.getElementById('detail-host'));
    var row = state.selected;
    if (!row) {
      host.appendChild(el('div', 'empty', '在左侧列表选择一条报销单'));
      return;
    }

    var kv = el('div', 'action-card');
    [
      ['单号', row.billNo],
      ['申请人', row.applicant + '（工号 ' + elText(row.employeeId) + '）'],
      ['部门', row.department],
      ['报销类型', row.rtype],
      ['金额', row.amount],
      ['事由', row.reason],
      ['票据状态', row.hasInvoice],
      ['城市', row.city],
      ['发票日期', fmtMs(row.invoiceDate)],
      ['提交时间', fmtMs(row.submittedAt)],
      ['合规结果', row.compliance],
      ['风险摘要', row.riskSummary],
      ['状态', row.status],
      ['审核人', row.reviewer],
      ['审批意见', row.reviewOpinion],
      ['审批时间', fmtMs(row.reviewedAt)],
      ['发票信息', row.invoiceInfo]
    ].forEach(function (pair) {
      var line = el('div', 'kv');
      line.appendChild(el('span', 'k', pair[0]));
      line.appendChild(el('span', 'v', elText(pair[1])));
      kv.appendChild(line);
    });
    host.appendChild(kv);
    host.appendChild(el('div', 'divider'));

    host.appendChild(reviewPanel(row));
    host.appendChild(el('div', 'divider'));
    host.appendChild(updatePanel(row));

    if (has('invoice.delete') || has('log.rollback')) {
      host.appendChild(el('div', 'divider'));
      host.appendChild(dangerPanel(row));
    }
  }

  //审核面板
  function reviewPanel(row) {
    var wrap = el('div', null);
    wrap.appendChild(el('div', 'hint', '审核（把状态与审批意见写回飞书，服务端会记录审核人）'));

    if (!has('invoice.review')) {
      wrap.appendChild(el('div', 'empty', '当前角色无审核权限'));
      return wrap;
    }

    var statusSelect = select(['审核中', '已通过', '已驳回'], '', '选择目标状态');

    var opinion = el('textarea');
    opinion.maxLength = 300;
    opinion.placeholder = '审批意见（2 到 300 字，会写入飞书并留审计）';

    var submit = el('button', 'primary small', '提交审核结果');
    submit.addEventListener('click', function () {
      if (!statusSelect.value) { U.toast('请选择目标状态', 'warn'); return; }
      var text = opinion.value.trim();
      if (text.length < 2) { U.toast('请填写审批意见（不少于 2 个字符）', 'warn'); return; }
      submit.disabled = true;
      API.post('/api/invoice/review', {
        recordId: row.recordId,
        status: statusSelect.value,
        opinion: text
      }).then(function (res) {
        toastMessage(res.data);
        submit.disabled = false;
        loadList();
      }).catch(function (err) {
        submit.disabled = false;
        reportError(err);
      });
    });

    wrap.appendChild(field('目标状态', statusSelect));
    wrap.appendChild(field('审批意见', opinion));
    wrap.appendChild(submit);
    return wrap;
  }

  //修改字段面板
  function updatePanel(row) {
    var wrap = el('div', null);
    wrap.appendChild(el('div', 'hint', '修改字段（仅财务可改字段，枚举必须从下拉选择）'));

    if (!has('invoice.update')) {
      wrap.appendChild(el('div', 'empty', '当前角色无修改权限'));
      return wrap;
    }

    var meta = state.meta || {};
    var editable = meta.financeEditableFields || [];
    var controls = [];

    editable.forEach(function (name) {
      var enumField = ENUM_FIELDS.filter(function (f) { return f.name === name; })[0];
      var control;
      if (enumField) {
        var options = meta[enumField.source] || [];
        control = select(options, row[valueKeyOf(name)] === undefined ? '' : String(row[valueKeyOf(name)]), '不修改');
      } else {
        control = el('input');
        control.type = name === '金额' ? 'number' : 'text';
        control.maxLength = 300;
        control.placeholder = name === '发票日期' ? 'yyyy-MM-dd（不改留空）' : '不改留空';
      }
      control.setAttribute('data-field', name);
      controls.push({ name: name, node: control });
      wrap.appendChild(field(name, control));
    });

    var submit = el('button', 'primary small', '提交修改');
    submit.addEventListener('click', function () {
      var fields = {};
      controls.forEach(function (c) {
        var value = String(c.node.value || '').trim();
        if (value !== '') fields[c.name] = value;
      });
      if (!Object.keys(fields).length) { U.toast('请至少填写一个要修改的字段', 'warn'); return; }
      submit.disabled = true;
      API.post('/api/invoice/update', { recordId: row.recordId, fields: fields }).then(function (res) {
        toastMessage(res.data);
        submit.disabled = false;
        loadList();
      }).catch(function (err) {
        submit.disabled = false;
        reportError(err);
      });
    });

    wrap.appendChild(submit);
    return wrap;
  }

  //字段名转数据键
  function valueKeyOf(fieldName) {
    var map = {
      '部门': 'department',
      '报销类型': 'rtype',
      '票据状态': 'hasInvoice',
      '城市': 'city',
      '合规结果': 'compliance',
      '状态': 'status',
      '金额': 'amount',
      '事由': 'reason',
      '发票日期': 'invoiceDate',
      '风险摘要': 'riskSummary'
    };
    return map[fieldName] || '';
  }

  //高风险操作面板
  function dangerPanel(row) {
    var wrap = el('div', null);
    wrap.appendChild(el('div', 'hint', '高风险操作（都会写审计，且可被管理员按日志回退）'));

    if (has('invoice.delete')) {
      var reason = el('textarea');
      reason.maxLength = 200;
      reason.placeholder = '删除理由（不少于 2 个字符）';
      var deleteBtn = el('button', 'danger small', '删除该报销单');
      deleteBtn.addEventListener('click', function () {
        var text = reason.value.trim();
        if (text.length < 2) { U.toast('请填写删除理由（不少于 2 个字符）', 'warn'); return; }
        deleteBtn.disabled = true;
        API.post('/api/invoice/delete', { recordId: row.recordId, reason: text }).then(function (res) {
          toastMessage(res.data);
          deleteBtn.disabled = false;
          state.selected = null;
          renderDetail();
          loadList();
        }).catch(function (err) {
          deleteBtn.disabled = false;
          reportError(err);
        });
      });
      wrap.appendChild(field('删除理由', reason));
      wrap.appendChild(deleteBtn);
    } else {
      wrap.appendChild(el('div', 'hint', '删除仅管理员及以上可用'));
    }

    if (has('log.rollback')) {
      wrap.appendChild(el('div', 'divider'));
      wrap.appendChild(el('div', 'hint', '按日志回退：到管理后台的「版本与回退」面板，对 kind 为 invoice 的版本执行回退'));
    }
    return wrap;
  }

  //AI 预审与批量助手（全部只出建议；执行只由下面的按钮触发）

  //拉取 AI 评分：默认命中缓存不重复花钱，refresh=true 才强制重算
  function loadScores(force) {
    var btn = document.getElementById('btn-ai-score');
    if (btn) { btn.disabled = true; btn.textContent = force ? '预审中…' : '加载中…'; }

    var path = '/api/invoice/ai-scores' + (force ? '?refresh=true' : '');
    API.get(path).then(function (res) {
      var data = res.data || {};
      state.scores = {};
      (data.rows || []).forEach(function (r) {
        if (r && r.hasScore) state.scores[r.recordId] = r;
      });
      state.aiStats = data.stats || null;
      state.aiAvailable = data.aiAvailable !== false;
      state.aiError = data.aiError || '';
      renderList();
      if (state.aiError) U.toast('AI 预审部分不可用：' + state.aiError, 'warn');
    }).catch(function (err) {
      U.toast('AI 预审失败：' + describeError(err), 'bad');
    }).then(function () {
      if (btn) { btn.disabled = false; btn.textContent = 'AI 预审'; }
    });
  }

  //让 AI 按需求选单（只出清单）
  function aiFind() {
    var input = document.getElementById('ai-req');
    var req = input.value.trim();
    if (req.length < 2) { U.toast('请先描述你要找的单（至少 2 个字）', 'warn'); return; }

    var btn = document.getElementById('btn-ai-find');
    btn.disabled = true; btn.textContent = '筛选…';

    API.post('/api/invoice/ai-find', { requirement: req }).then(function (res) {
      var data = res.data || {};
      state.aiPicked = (data.picked || []).map(function (p) {
        return {
          recordId: p.recordId,
          billNo: p.billNo || '',
          summary: p.summary || '',
          level: p.level || 0,
          levelLabel: p.levelLabel || '',
          reason: p.reason || '',
          checked: true
        };
      });
      renderAiPicks(data);
      U.toast('AI 已列出 ' + state.aiPicked.length + ' 条候选，尚未执行任何操作', 'info');
    }).catch(function (err) {
      U.toast('AI 找单失败：' + describeError(err), 'bad');
    }).then(function () { btn.disabled = false; btn.textContent = '让 AI 找单'; });
  }

  //渲染 AI 候选清单（可勾选，勾选状态由财务掌控）
  function renderAiPicks(data) {
    data = data || {};
    var note = document.getElementById('ai-find-note');
    var host = U.clear(document.getElementById('ai-pick-host'));
    var approveBtn = document.getElementById('btn-ai-approve');
    var rejectBtn = document.getElementById('btn-ai-reject');
    var clearBtn = document.getElementById('btn-ai-clear');

    var actionLabel = data.action === 'approve' ? '建议通过'
      : data.action === 'reject' ? '建议打回' : '需求不明确';

    if (!state.aiPicked.length) {
      note.textContent = data.note ? String(data.note) : '';
      host.appendChild(el('div', 'empty', '暂无候选清单'));
      approveBtn.hidden = true; rejectBtn.hidden = true; clearBtn.hidden = true;
      return;
    }

    var dropped = data.dropped || [];
    note.textContent = (data.note ? data.note + '；' : '') +
      'AI ' + actionLabel + '，候选 ' + state.aiPicked.length + ' 条' +
      (dropped.length ? '，已剔除 ' + dropped.length + ' 条（不存在或状态已变）' : '') +
      '\n提示：勾选后点按钮才会真正执行；每单独立留快照可回退。';

    state.aiPicked.forEach(function (item, index) {
      var row = document.createElement('div');
      row.className = 'ai-pick-item';

      var box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = item.checked;
      box.addEventListener('change', function () { state.aiPicked[index].checked = box.checked; });
      row.appendChild(box);

      var txt = el('div', 'txt');
      var head = el('div', 'pick-head');
      //优先显示单号与摘要（recordId 是哈希串，人认不出来）
      head.appendChild(el('span', 'pick-title',
        item.summary || item.billNo || elText(item.recordId)));
      if (item.level >= 1 && item.level <= 4) head.appendChild(badge(lvlText(item.level), lvlClass(item.level)));
      txt.appendChild(head);

      if (!item.summary && item.billNo) txt.appendChild(el('div', 'why', '单号：' + item.billNo));
      if (item.reason) txt.appendChild(el('div', 'why', 'AI：' + item.reason));
      txt.appendChild(el('div', 'pick-id', '记录 ID（后台用）：' + elText(item.recordId)));
      row.appendChild(txt);

      host.appendChild(row);
    });

    approveBtn.hidden = false;
    rejectBtn.hidden = false;
    clearBtn.hidden = false;
  }

  //批量执行：唯一写入口，必须财务本人点击
  function aiBatch(action) {
    var ids = state.aiPicked.filter(function (p) { return p.checked; })
      .map(function (p) { return p.recordId; });
    if (!ids.length) { U.toast('请先勾选要处理的单据', 'warn'); return; }

    var head = action === 'approve' ? '全部通过' : '全部打回';
    var ok = window.confirm('将对勾选的 ' + ids.length + ' 单执行「' + head + '」。\n' +
      '每单都会独立留存快照与审计，可单独回退。是否继续？');
    if (!ok) return;

    API.post('/api/invoice/batch-review', {
      recordIds: ids,
      action: action,
      opinion: '财务核对 AI 清单后批量处理'
    }).then(function (res) {
      var d = res.data || {};
      U.toast(d.message || '已处理', d.failed ? 'warn' : 'ok');
      state.aiPicked = [];
      renderAiPicks({});
      loadList();
      loadScores(false);
    }).catch(function (err) {
      U.toast('批量处理失败：' + describeError(err), 'bad');
    });
  }

  //筛选与事件绑定

  //读取筛选条件
  function readFilters() {
    state.filters.status = document.getElementById('filter-status').value;
    state.filters.type = document.getElementById('filter-type').value;
    state.filters.department = document.getElementById('filter-department').value;
    state.filters.keyword = document.getElementById('filter-keyword').value.trim();
  }

  //绑定页面事件
  function bind() {
    document.getElementById('btn-login').addEventListener('click', login);
    document.getElementById('login-pass').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') login();
    });
    document.getElementById('btn-deny-logout').addEventListener('click', function () {
      API.post('/api/auth/logout', {}).catch(function () { /* 忽略 */ }).then(function () {
        API.clear();
        showLogin('已退出登录');
      });
    });
    document.getElementById('btn-logout').addEventListener('click', function () {
      API.post('/api/auth/logout', {}).catch(function () { /* 忽略 */ }).then(function () {
        API.clear();
        showLogin('已退出登录');
      });
    });
    document.getElementById('btn-refresh').addEventListener('click', function () { loadList(); });

    //AI 预审与批量助手
    document.getElementById('btn-ai-score').addEventListener('click', function () { loadScores(true); });
    document.getElementById('btn-ai-find').addEventListener('click', aiFind);
    document.getElementById('btn-ai-approve').addEventListener('click', function () { aiBatch('approve'); });
    document.getElementById('btn-ai-reject').addEventListener('click', function () { aiBatch('reject'); });
    document.getElementById('btn-ai-clear').addEventListener('click', function () {
      state.aiPicked = [];
      document.getElementById('ai-req').value = '';
      renderAiPicks({});
    });
    document.getElementById('ai-req').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') aiFind();
    });
    document.getElementById('btn-clear-filter').addEventListener('click', function () {
      document.getElementById('filter-status').value = '';
      document.getElementById('filter-type').value = '';
      document.getElementById('filter-department').value = '';
      document.getElementById('filter-keyword').value = '';
      readFilters();
      renderList();
    });

    ['filter-status', 'filter-type', 'filter-department'].forEach(function (id) {
      document.getElementById(id).addEventListener('change', function () {
        readFilters();
        renderList();
      });
    });
    document.getElementById('filter-keyword').addEventListener('input', function () {
      readFilters();
      renderList();
    });

    window.addEventListener('session-expired', function () { showLogin('登录状态已失效，请重新登录'); });
  }

  bind();
  boot();
})();
