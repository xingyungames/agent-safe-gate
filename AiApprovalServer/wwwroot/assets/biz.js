/*
 * biz.js —— 业务对接台（甲方 / 乙方 / 管理员）。
 *
 * 设计原则（与 app.js 保持一致，也是本页敢做成"按钮表单"的前提）：
 *   1) 页面上的每个写按钮都只做一件事：把一个受约束的工具调用 POST 给 /api/biz/action。
 *      脚本里**没有**任何"直接改数据"的通道，也没有任何"批准"按钮——审批权限只在管理员后台。
 *   2) 渲染一律走 U.el + textContent 构造 DOM，绝不把字符串当 HTML 插入，服务端返回的数据全部当作文本处理。
 *   3) 前端校验只是为了少一次无效往返，**服务端的规则引擎才是权威**：任何绕过前端的请求照样会被校验。
 */
(function () {
  'use strict';

  var el = U.el;

  // ------------------------------------------------------------------ 领域常量
  var INDUSTRIES = ['制造', '科技', '物流', '教育', '传媒', '金融', '其它'];
  var LEVELS = ['普通', '白银', '黄金', '铂金'];
  var CLIENT_STATUS = ['潜在', '洽谈中', '合作中', '已暂停', '已终止'];
  var REQ_STATUS = ['待评估', '已报价', '进行中', '已交付', '已关闭'];

  // 各角色可修改的字段（必须与服务端白名单一致；前端只是提前把可选项列出来）
  var SELF_FIELDS = ['name', 'contact', 'phone', 'industry', 'requirement'];
  var STAFF_CLIENT_FIELDS = ['name', 'contact', 'phone', 'industry', 'level', 'status', 'requirement', 'remark'];
  var REQ_FIELDS = ['title', 'detail', 'budget', 'expect_date', 'status', 'owner'];

  var FIELD_META = {
    name: { label: '公司名称', control: 'text', max: 60 },
    contact: { label: '联系人', control: 'text', max: 30 },
    phone: { label: '联系电话', control: 'phone', max: 11 },
    industry: { label: '所属行业', control: 'enum', options: INDUSTRIES },
    level: { label: '客户等级', control: 'enum', options: LEVELS },
    status: { label: '合作状态', control: 'enum', options: CLIENT_STATUS },
    requirement: { label: '需求摘要', control: 'textarea', max: 200 },
    remark: { label: '备注', control: 'textarea', max: 200 },
    title: { label: '标题', control: 'text', max: 60 },
    detail: { label: '描述', control: 'textarea', max: 500 },
    budget: { label: '预算（元）', control: 'number' },
    expect_date: { label: '期望交付日', control: 'date' },
    owner: { label: '负责人', control: 'text', max: 30 }
  };

  var state = {
    overview: null,
    clients: [],
    requirements: [],
    filters: { keyword: '', industry: '', level: '', status: '' },
    filtersReady: false
  };

  function metaOf(field, kind) {
    if (kind === 'requirement' && field === 'status') return { label: '需求状态', control: 'enum', options: REQ_STATUS };
    return FIELD_META[field] || { label: field, control: 'text' };
  }

  // ------------------------------------------------------------------ 基础控件
  function input(type, id, placeholder, maxLength) {
    var node = el('input');
    node.type = type || 'text';
    if (id) node.id = id;
    if (placeholder) node.placeholder = placeholder;
    if (maxLength) node.maxLength = maxLength;
    return node;
  }

  function textarea(placeholder, maxLength) {
    var node = el('textarea');
    if (placeholder) node.placeholder = placeholder;
    if (maxLength) node.maxLength = maxLength;
    return node;
  }

  function selectPairs(pairs, emptyLabel) {
    var node = el('select');
    if (emptyLabel) {
      var blank = el('option', null, emptyLabel);
      blank.value = '';
      node.appendChild(blank);
    }
    pairs.forEach(function (pair) {
      var option = el('option', null, pair[1]);
      option.value = pair[0];
      node.appendChild(option);
    });
    return node;
  }

  function selectOf(values, emptyLabel) {
    return selectPairs(values.map(function (v) { return [v, v]; }), emptyLabel);
  }

  function field(labelText, control, hintText) {
    var wrap = el('label', 'field');
    wrap.appendChild(el('span', null, labelText));
    wrap.appendChild(control);
    if (hintText) wrap.appendChild(el('span', 'hint', hintText));
    return wrap;
  }

  function controlValue(control) {
    if (!control || control.value === undefined || control.value === null) return '';
    return String(control.value).trim();
  }

  function pairsTable(pairs) {
    var table = el('table');
    var body = el('tbody');
    pairs.forEach(function (pair) {
      if (pair[1] === undefined || pair[1] === null) return;
      var row = el('tr');
      row.appendChild(el('th', null, pair[0]));
      row.appendChild(el('td', null, pair[1] === '' ? '-' : pair[1]));
      body.appendChild(row);
    });
    table.appendChild(body);
    return table;
  }

  function viewTable(columns, rows, onRowClick) {
    var wrap = el('div', 'list');
    if (!rows || !rows.length) {
      wrap.appendChild(el('div', 'empty', '暂无数据'));
      return wrap;
    }

    var table = el('table');
    var thead = el('thead');
    var headRow = el('tr');
    columns.forEach(function (c) { headRow.appendChild(el('th', null, c.title)); });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var body = el('tbody');
    rows.forEach(function (row) {
      var tr = el('tr');
      columns.forEach(function (c) {
        var td = el('td');
        var value = c.render ? c.render(row) : row[c.key];
        if (value && value.nodeType) td.appendChild(value);
        else td.textContent = (value === undefined || value === null || value === '') ? '-' : String(value);
        tr.appendChild(td);
      });
      if (onRowClick) tr.addEventListener('click', function () { onRowClick(row); });
      body.appendChild(tr);
    });
    table.appendChild(body);
    wrap.appendChild(table);
    return wrap;
  }

  function badge(text, cls) { return el('span', 'badge ' + (cls || 'info'), text); }

  // ------------------------------------------------------------------ 弹层
  function closeModal() {
    var host = document.getElementById('modal-host');
    if (host) U.clear(host);
  }

  function openModal(title, bodyNode, footerNode) {
    var host = U.clear(document.getElementById('modal-host'));
    var mask = el('div', 'modal-mask');
    var modal = el('div', 'modal');

    var head = el('div', 'modal-head');
    head.appendChild(el('span', null, title));
    head.appendChild(el('span', 'grow'));
    var close = el('button', 'small ghost', '关闭');
    close.type = 'button';
    close.addEventListener('click', closeModal);
    head.appendChild(close);

    var body = el('div', 'modal-body');
    body.appendChild(bodyNode);
    modal.appendChild(head);
    modal.appendChild(body);

    if (footerNode) {
      var foot = el('div', 'modal-foot');
      foot.appendChild(footerNode);
      modal.appendChild(foot);
    }

    mask.appendChild(modal);
    mask.addEventListener('click', function (e) { if (e.target === mask) closeModal(); });
    host.appendChild(mask);
  }

  // ------------------------------------------------------------------ 启动 / 登录
  function boot() {
    if (API.isLoggedIn()) loadAll();
    else showLogin();
  }

  function showLogin() {
    document.getElementById('login-view').hidden = false;
    document.getElementById('biz-view').hidden = true;
    document.getElementById('userbox').hidden = true;
    closeModal();
    API.clear();
  }

  function enterBiz() {
    document.getElementById('login-view').hidden = true;
    document.getElementById('biz-view').hidden = false;
    document.getElementById('userbox').hidden = false;
  }

  function login() {
    var userName = document.getElementById('login-user').value.trim();
    var password = document.getElementById('login-pass').value;
    if (!userName || !password) { U.toast('请输入用户名与口令', 'warn'); return; }

    var button = document.getElementById('btn-login');
    button.disabled = true;

    API.public('/api/auth/login', { userName: userName, password: password }).then(function (res) {
      API.setSession(res.data);
      document.getElementById('login-pass').value = '';
      U.toast('登录成功', 'ok');
      enterBiz();
      return loadAll();
    }).catch(function (err) {
      U.toast(err.message, 'bad');
    }).then(function () { button.disabled = false; });
  }

  function logout() {
    API.post('/api/auth/logout', {}).catch(function () { /* 退出失败也继续清理本地状态 */ }).then(function () {
      state.overview = null;
      state.clients = [];
      state.requirements = [];
      showLogin();
      U.toast('已退出登录', 'ok');
    });
  }

  // ------------------------------------------------------------------ 加载总览
  function loadAll() {
    U.clear(document.getElementById('main-error'));
    return API.get('/api/biz/overview').then(function (res) {
      state.overview = res.data || {};
      enterBiz();
      renderTopbar();
      renderRole();
      return loadTasks();
    }).catch(function (err) {
      var message = '业务接口调用失败：' + err.message + (err.code ? '（' + err.code + '）' : '');
      if (err.status === 404) message += '。服务端可能尚未部署业务前台接口。';
      var host = U.clear(document.getElementById('main-error'));
      host.appendChild(el('div', 'banner bad', message));
      var taskHost = U.clear(document.getElementById('task-list'));
      taskHost.appendChild(el('div', 'empty', '等待业务接口可用后再试'));
    });
  }

  function renderTopbar() {
    var user = API.session().user || {};
    var overview = state.overview || {};
    var role = overview.role || user.role || 'user';

    document.getElementById('userbox').hidden = false;
    document.getElementById('whoami').textContent =
      (user.displayName || user.userName || '当前用户') + '（' + (user.userName || '-') + '）';

    var roleBadge = document.getElementById('rolebadge');
    roleBadge.textContent = overview.roleLabel || user.roleLabel || role;
    roleBadge.className = 'badge ' + (role === 'admin' ? 'bad' : role === 'staff' ? 'info' : 'ok');

    document.getElementById('nav-admin').hidden = role !== 'admin';

    var ai = document.getElementById('ai-badge');
    var aiStatus = overview.aiStatus || 'off';
    if (aiStatus === 'online') { ai.textContent = 'AI 在线'; ai.className = 'badge ok'; }
    else if (aiStatus === 'degraded') { ai.textContent = 'AI 降级'; ai.className = 'badge warn'; }
    else { ai.textContent = '本地模式'; ai.className = 'badge warn'; }
  }

  function renderRole() {
    var overview = state.overview || {};
    var role = overview.role || 'user';

    document.getElementById('role-user').hidden = role !== 'user';
    document.getElementById('role-staff').hidden = role !== 'staff';
    document.getElementById('role-admin').hidden = role !== 'admin';

    if (role === 'staff') renderStaff(overview);
    else if (role === 'admin') renderAdmin(overview);
    else renderUser(overview);
  }

  // ------------------------------------------------------------------ 甲方（用户角色）
  function renderUser(overview) {
    var self = overview.self || {};
    var client = self.client || null;
    var linked = !!self.linked;

    var stateBadge = document.getElementById('profile-state');
    stateBadge.textContent = linked ? '已建档' : '尚未建档';
    stateBadge.className = 'badge ' + (linked ? 'ok' : 'warn');

    var body = U.clear(document.getElementById('profile-body'));
    var actions = U.clear(document.getElementById('profile-actions'));
    var createCard = document.getElementById('card-create');

    if (client) body.appendChild(pairsTable(clientPairs(client)));
    else body.appendChild(el('div', 'empty', '还没有我司档案。请先在下面提交建档申请，审批通过后这里会显示档案信息。'));

    if (linked) {
      createCard.hidden = true;
      actions.hidden = false;
      var editButton = el('button', 'primary small', '申请修改我司档案');
      editButton.type = 'button';
      editButton.addEventListener('click', function () {
        openFieldEditor({
          title: '申请修改我司档案',
          fields: SELF_FIELDS,
          kind: 'client',
          current: client,
          submit: function (fieldKey, value, reason, button) {
            act('update_client_profile', { field: fieldKey, value: value, reason: reason }, reason, button);
          }
        });
      });
      actions.appendChild(editButton);
      actions.appendChild(el('span', 'hint', '修改需要填写理由；提交后会作为一条申请进入人工审批，审批通过才生效。'));
    } else {
      createCard.hidden = false;
      actions.hidden = true;
      renderCreateForm();
    }

    renderUserRequirements(overview.myRequirements || []);
  }

  function clientPairs(client) {
    return [
      ['客户编号', client.id],
      ['公司名称', client.name],
      ['联系人', client.contact],
      ['联系电话', client.phone],
      ['所属行业', client.industry],
      ['客户等级', client.level],
      ['合作状态', client.status],
      ['负责专员', client.owner],
      ['需求摘要', client.requirement],
      ['备注', client.remark],
      ['更新时间', client.updated_at]
    ];
  }

  function renderCreateForm() {
    var host = U.clear(document.getElementById('create-form'));

    var name = input('text', 'create-name', '例如：星云游科技有限公司', 60);
    var contact = input('text', 'create-contact', '联系人姓名', 30);
    var phone = input('text', 'create-phone', '11 位手机号，例如 13800000000', 11);
    var industry = selectOf(INDUSTRIES, false);
    var requirement = textarea('简要描述贵司的需求方向（可选）', 200);

    host.appendChild(field('公司名称', name));
    host.appendChild(field('联系人', contact));
    host.appendChild(field('联系电话', phone, '必须是 11 位手机号'));
    host.appendChild(field('所属行业', industry));
    host.appendChild(field('需求摘要', requirement));

    var submit = el('button', 'primary', '提交建档申请');
    submit.type = 'button';
    submit.addEventListener('click', function () {
      var args = {
        name: controlValue(name),
        contact: controlValue(contact),
        phone: controlValue(phone)
      };
      var industryValue = controlValue(industry);
      var requirementValue = controlValue(requirement);

      var error = validateField('name', args.name, 'client') ||
                  validateField('contact', args.contact, 'client') ||
                  validateField('phone', args.phone, 'client');
      if (error) { U.toast(error, 'warn'); return; }

      if (industryValue) args.industry = industryValue;
      if (requirementValue) args.requirement = requirementValue;

      act('create_client_profile', args, null, submit, { reason: '业务对接台：甲方提交建档申请' });
    });

    var row = el('div', 'row');
    row.appendChild(submit);
    row.appendChild(el('span', 'hint', '提交后不会立即生效：先过规则引擎与安全审核，再转管理员人工审批。'));
    host.appendChild(row);
  }

  function renderUserRequirements(list) {
    document.getElementById('req-count').textContent = list.length + ' 条';
    renderRequirementForm();

    var host = U.clear(document.getElementById('req-list'));
    if (!list.length) {
      host.appendChild(el('div', 'empty', '还没有提交过需求单'));
      return;
    }

    host.appendChild(viewTable([
      { title: '需求号', key: 'id' },
      { title: '标题', key: 'title' },
      { title: '状态', render: function (r) { return badge(r.status || '-', 'info'); } },
      { title: '预算（元）', key: 'budget' },
      { title: '期望交付', key: 'expect_date' },
      { title: '负责人', key: 'owner' },
      { title: '提交时间', render: function (r) { return U.fmtTime(r.created_at); } }
    ], list, function (row) { openRequirementDetail(row, false); }));
  }

  function renderRequirementForm() {
    var host = U.clear(document.getElementById('req-form'));

    var title = input('text', 'req-title', '例如：客户档案批量导入工具', 60);
    var detail = textarea('描述业务背景、目标与验收标准（必填）', 500);
    var budget = input('number', 'req-budget', '预算（元，可选）');
    var expectDate = input('date', 'req-expect-date', null);

    host.appendChild(el('div', 'hint', '提交新需求单'));
    host.appendChild(field('标题', title));
    host.appendChild(field('描述', detail));
    host.appendChild(field('预算（元）', budget));
    host.appendChild(field('期望交付日', expectDate, '格式 yyyy-MM-dd'));

    var submit = el('button', 'primary', '提交需求单');
    submit.type = 'button';
    submit.addEventListener('click', function () {
      var args = { title: controlValue(title), detail: controlValue(detail) };
      var budgetValue = controlValue(budget);
      var dateValue = controlValue(expectDate);

      var error = validateField('title', args.title, 'requirement') ||
                  validateField('detail', args.detail, 'requirement');
      if (!error && budgetValue) error = validateField('budget', budgetValue, 'requirement');
      if (!error && dateValue) error = validateField('expect_date', dateValue, 'requirement');
      if (error) { U.toast(error, 'warn'); return; }

      if (budgetValue) args.budget = budgetValue;
      if (dateValue) args.expect_date = dateValue;

      act('submit_requirement', args, null, submit, { reason: '业务对接台：甲方提交需求单' });
    });

    var row = el('div', 'row');
    row.appendChild(submit);
    row.appendChild(el('span', 'hint', '需求单同样需要人工审批后才进入受理流程。'));
    host.appendChild(row);
  }

  function openRequirementDetail(req, withMaintainButton) {
    var body = el('div');
    body.appendChild(pairsTable([
      ['需求号', req.id],
      ['客户编号', req.customer_id],
      ['标题', req.title],
      ['描述', req.detail],
      ['预算（元）', req.budget],
      ['期望交付日', req.expect_date],
      ['状态', req.status],
      ['负责人', req.owner],
      ['提交时间', U.fmtTime(req.created_at)],
      ['更新时间', U.fmtTime(req.updated_at)]
    ]));

    var footer = el('div', 'row tight');
    if (withMaintainButton) {
      var maintain = el('button', 'primary', '维护需求单');
      maintain.type = 'button';
      maintain.addEventListener('click', function () {
        openFieldEditor({
          title: '维护需求单 ' + (req.id || ''),
          fields: REQ_FIELDS,
          kind: 'requirement',
          current: req,
          submit: function (fieldKey, value, reason, button) {
            act('update_requirement', { id: req.id, field: fieldKey, value: value, reason: reason }, reason, button);
          }
        });
      });
      footer.appendChild(maintain);
    }
    footer.appendChild(el('span', 'hint', withMaintainButton ? '维护需求单需要填写理由，并经人工审批。' : '如需变更，请联系乙方运营人员发起维护申请。'));

    openModal('需求单详情 ' + (req.id || ''), body, footer);
  }

  // ------------------------------------------------------------------ 乙方（员工角色）
  function renderStaff(overview) {
    state.clients = overview.clients || [];
    state.requirements = overview.requirements || [];

    prepareFilters();
    renderClientTable();
    renderStaffRequirements();
  }

  function prepareFilters() {
    if (state.filtersReady) return;

    var industry = document.getElementById('filter-industry');
    var level = document.getElementById('filter-level');
    var status = document.getElementById('filter-status');

    U.clear(industry).appendChild(selectOf(INDUSTRIES, '全部行业'));
    U.clear(level).appendChild(selectOf(LEVELS, '全部等级'));
    U.clear(status).appendChild(selectOf(CLIENT_STATUS, '全部状态'));

    // 让下拉显示占位值
    industry.value = '';
    level.value = '';
    status.value = '';

    state.filtersReady = true;
  }

  function applyFilters() {
    var f = state.filters;
    var keyword = (f.keyword || '').toLowerCase();

    return state.clients.filter(function (c) {
      if (f.industry && (c.industry || '') !== f.industry) return false;
      if (f.level && (c.level || '') !== f.level) return false;
      if (f.status && (c.status || '') !== f.status) return false;
      if (!keyword) return true;
      var haystack = ((c.name || '') + ' ' + (c.contact || '') + ' ' + (c.owner || '')).toLowerCase();
      return haystack.indexOf(keyword) >= 0;
    });
  }

  function requirementCountOf(clientId) {
    return state.requirements.filter(function (r) { return r.customer_id === clientId; }).length;
  }

  function renderClientTable() {
    var rows = applyFilters();
    var host = U.clear(document.getElementById('staff-client-table'));
    document.getElementById('staff-client-count').textContent = rows.length + ' 家';

    if (!state.clients.length) {
      host.appendChild(el('div', 'empty', '暂无客户档案'));
      return;
    }
    if (!rows.length) {
      host.appendChild(el('div', 'empty', '没有符合当前筛选条件的客户'));
      return;
    }

    host.appendChild(viewTable([
      { title: '客户编号', key: 'id' },
      { title: '公司名称', key: 'name' },
      { title: '联系人', key: 'contact' },
      { title: '联系电话', key: 'phone' },
      { title: '所属行业', key: 'industry' },
      { title: '等级', render: function (r) { return badge(r.level || '-', 'ok'); } },
      { title: '合作状态', render: function (r) { return badge(r.status || '-', 'info'); } },
      { title: '负责专员', key: 'owner' },
      { title: '需求数', render: function (r) { return String(requirementCountOf(r.id)); } }
    ], rows, function (row) { openClientDetail(row); }));
  }

  function openClientDetail(client) {
    var body = el('div');
    body.appendChild(pairsTable(clientPairs(client)));

    body.appendChild(el('div', 'divider'));
    var related = state.requirements.filter(function (r) { return r.customer_id === client.id; });
    body.appendChild(el('div', 'hint', '该客户的需求单（' + related.length + ' 条）'));
    body.appendChild(viewTable([
      { title: '需求号', key: 'id' },
      { title: '标题', key: 'title' },
      { title: '状态', render: function (r) { return badge(r.status || '-', 'info'); } },
      { title: '预算（元）', key: 'budget' },
      { title: '负责人', key: 'owner' }
    ], related, function (row) { openRequirementDetail(row, true); }));

    var footer = el('div', 'row tight');

    var editButton = el('button', 'primary small', '申请修改客户信息');
    editButton.type = 'button';
    editButton.addEventListener('click', function () {
      openFieldEditor({
        title: '申请修改客户信息：' + (client.name || client.id || ''),
        fields: STAFF_CLIENT_FIELDS,
        kind: 'client',
        current: client,
        submit: function (fieldKey, value, reason, button) {
          act('update_client_profile', {
            client_id: client.id,
            field: fieldKey,
            value: value,
            reason: reason
          }, reason, button);
        }
      });
    });

    var assignButton = el('button', 'small', '指派负责员工');
    assignButton.type = 'button';
    assignButton.addEventListener('click', function () { openAssignOwner(client); });

    var requirementButton = el('button', 'small', '为该客户提交需求单');
    requirementButton.type = 'button';
    requirementButton.addEventListener('click', function () { openStaffRequirementForm(client); });

    footer.appendChild(editButton);
    footer.appendChild(assignButton);
    footer.appendChild(requirementButton);
    footer.appendChild(el('span', 'hint', '三个操作都会转人工审批。'));

    openModal('客户详情 ' + (client.id || ''), body, footer);
  }

  function openAssignOwner(client) {
    var body = el('div');
    body.appendChild(el('div', 'hint', '客户：' + (client.name || '-') + '（当前负责人：' + (client.owner || '未指派') + '）'));

    var owner = input('text', 'assign-owner', '负责员工的用户名，例如 lisi', 30);
    var reason = textarea('指派理由（必填，建议写明交接背景）', 200);

    body.appendChild(field('负责员工', owner));
    body.appendChild(field('理由', reason));

    var footer = el('div', 'row tight');
    var submit = el('button', 'primary', '提交指派申请');
    submit.type = 'button';
    submit.addEventListener('click', function () {
      var ownerValue = controlValue(owner);
      var reasonValue = controlValue(reason);

      var error = validateField('owner', ownerValue, 'client');
      if (error) { U.toast(error, 'warn'); return; }
      if (reasonValue.length < 2) { U.toast('请填写指派理由（不少于 2 个字符）', 'warn'); return; }

      act('assign_client_owner', {
        client_id: client.id,
        owner: ownerValue,
        reason: reasonValue
      }, reasonValue, submit);
    });
    footer.appendChild(submit);
    footer.appendChild(el('span', 'hint', '指派同样需要管理员审批后生效。'));

    openModal('指派负责员工：' + (client.id || ''), body, footer);
  }

  function openStaffRequirementForm(client) {
    var body = el('div');
    body.appendChild(el('div', 'hint', '为客户 ' + (client.name || '-') + '（' + (client.id || '-') + '）提交需求单'));

    var title = input('text', 'staff-req-title', '需求标题', 60);
    var detail = textarea('需求描述（必填）', 500);
    var budget = input('number', 'staff-req-budget', '预算（元，可选）');
    var expectDate = input('date', 'staff-req-date', null);

    body.appendChild(field('标题', title));
    body.appendChild(field('描述', detail));
    body.appendChild(field('预算（元）', budget));
    body.appendChild(field('期望交付日', expectDate, '格式 yyyy-MM-dd'));

    var footer = el('div', 'row tight');
    var submit = el('button', 'primary', '提交需求单');
    submit.type = 'button';
    submit.addEventListener('click', function () {
      var args = {
        title: controlValue(title),
        detail: controlValue(detail),
        customer_id: client.id
      };
      var budgetValue = controlValue(budget);
      var dateValue = controlValue(expectDate);

      var error = validateField('title', args.title, 'requirement') ||
                  validateField('detail', args.detail, 'requirement');
      if (!error && budgetValue) error = validateField('budget', budgetValue, 'requirement');
      if (!error && dateValue) error = validateField('expect_date', dateValue, 'requirement');
      if (error) { U.toast(error, 'warn'); return; }

      if (budgetValue) args.budget = budgetValue;
      if (dateValue) args.expect_date = dateValue;

      act('submit_requirement', args, null, submit, { reason: '业务对接台：员工代客户提交需求单' });
    });
    footer.appendChild(submit);

    openModal('为客户提交需求单', body, footer);
  }

  function renderStaffRequirements() {
    var list = state.requirements;
    document.getElementById('staff-req-count').textContent = list.length + ' 条';

    var host = U.clear(document.getElementById('staff-req-table'));
    if (!list.length) {
      host.appendChild(el('div', 'empty', '暂无需求单'));
      return;
    }

    host.appendChild(viewTable([
      { title: '需求号', key: 'id' },
      { title: '客户编号', key: 'customer_id' },
      { title: '标题', key: 'title' },
      { title: '状态', render: function (r) { return badge(r.status || '-', 'info'); } },
      { title: '预算（元）', key: 'budget' },
      { title: '期望交付', key: 'expect_date' },
      { title: '负责人', key: 'owner' },
      { title: '操作', render: function (r) {
          var button = el('button', 'small ghost', '维护');
          button.type = 'button';
          button.addEventListener('click', function (e) {
            e.stopPropagation();
            openFieldEditor({
              title: '维护需求单 ' + (r.id || ''),
              fields: REQ_FIELDS,
              kind: 'requirement',
              current: r,
              submit: function (fieldKey, value, reason, btn) {
                act('update_requirement', { id: r.id, field: fieldKey, value: value, reason: reason }, reason, btn);
              }
            });
          });
          return button;
        } }
    ], list, function (row) { openRequirementDetail(row, true); }));
  }

  // ------------------------------------------------------------------ 管理员（只读）
  function renderAdmin(overview) {
    state.clients = overview.clients || [];
    state.requirements = overview.requirements || [];
    var stats = overview.stats || {};

    var host = U.clear(document.getElementById('admin-stats'));
    var grid = el('div', 'stat-grid');
    [
      ['客户总数', stats.clientCount],
      ['需求单总数', stats.requirementCount],
      ['待人工审批', stats.pendingCount]
    ].forEach(function (item) {
      var box = el('div', 'stat');
      box.appendChild(el('div', 'label', item[0]));
      box.appendChild(el('div', 'value', item[1] === undefined || item[1] === null ? '-' : String(item[1])));
      grid.appendChild(box);
    });
    host.appendChild(grid);
    host.appendChild(el('div', 'divider'));
    host.appendChild(el('p', 'hint',
      '本页对管理员只读：客户档案与需求池的浏览不产生写操作，因此不需要审批。' +
      '审批、回退、用户管理请到管理后台操作。'));

    var clientHost = U.clear(document.getElementById('admin-client-table'));
    clientHost.appendChild(viewTable([
      { title: '客户编号', key: 'id' },
      { title: '公司名称', key: 'name' },
      { title: '联系人', key: 'contact' },
      { title: '联系电话', key: 'phone' },
      { title: '所属行业', key: 'industry' },
      { title: '等级', key: 'level' },
      { title: '合作状态', key: 'status' },
      { title: '负责专员', key: 'owner' },
      { title: '更新时间', key: 'updated_at' }
    ], state.clients));

    var requirementHost = U.clear(document.getElementById('admin-req-table'));
    requirementHost.appendChild(viewTable([
      { title: '需求号', key: 'id' },
      { title: '客户编号', key: 'customer_id' },
      { title: '标题', key: 'title' },
      { title: '预算（元）', key: 'budget' },
      { title: '期望交付', key: 'expect_date' },
      { title: '状态', key: 'status' },
      { title: '负责人', key: 'owner' },
      { title: '提交时间', render: function (r) { return U.fmtTime(r.created_at); } }
    ], state.requirements));
  }

  // ------------------------------------------------------------------ 我的操作记录
  function loadTasks() {
    return API.get('/api/agent/tasks').then(function (res) {
      var list = (res.data || []).slice(0, 20);
      renderTasks(list);
      return list;
    }).catch(function (err) {
      var host = U.clear(document.getElementById('task-list'));
      host.appendChild(el('div', 'banner bad', '读取操作记录失败：' + err.message));
    });
  }

  function renderTasks(list) {
    document.getElementById('task-count').textContent = list.length + ' 条';
    var host = U.clear(document.getElementById('task-list'));

    if (!list.length) {
      host.appendChild(el('div', 'empty', '还没有操作记录'));
      return;
    }

    list.forEach(function (task) {
      var item = el('div', 'list-item');
      item.appendChild(el('div', 'title', task.intent || task.tool || '操作'));

      var meta = el('div', 'meta');
      meta.appendChild(badge(task.statusLabel || task.status || '-', U.statusClass(task.status)));
      meta.appendChild(document.createTextNode(
        ' ' + (task.tool || '-') + ' · 风险 ' + (task.riskLabel || '-') + ' · ' + U.fmtTime(task.createdAt)));
      item.appendChild(meta);

      item.addEventListener('click', function () { openTaskDetail(task); });
      host.appendChild(item);
    });
  }

  function openTaskDetail(task) {
    var body = el('div');
    body.appendChild(pairsTable([
      ['任务号', task.id],
      ['状态', (task.statusLabel || '-') + '（' + (task.status || '-') + '）'],
      ['工具', task.tool],
      ['意图摘要', task.intent],
      ['风险等级', task.riskLabel],
      ['创建时间', U.fmtTime(task.createdAt)],
      ['裁决人', task.decidedBy],
      ['审批意见', task.decisionReason]
    ]));

    body.appendChild(el('div', 'divider'));
    body.appendChild(el('div', 'hint', '规范化动作（执行端唯一依据）'));
    body.appendChild(el('pre', 'code', task.action ? U.json(task.action) : '（无动作参数：该请求在执行前就被拦截）'));

    if (task.result) {
      body.appendChild(el('div', 'divider'));
      body.appendChild(el('div', 'hint', '执行结果'));
      body.appendChild(el('pre', 'code', U.json(task.result)));
    }

    openModal('操作详情 ' + (task.id || ''), body);
  }

  // ------------------------------------------------------------------ 修改类表单（字段 + 新值 + 理由）
  function openFieldEditor(cfg) {
    var body = el('div');
    if (cfg.current) {
      body.appendChild(el('div', 'hint', '当前记录：' + (cfg.current.name || cfg.current.title || cfg.current.id || '-')));
    }

    var fieldSelect = selectPairs(cfg.fields.map(function (f) { return [f, metaOf(f, cfg.kind).label]; }), null);
    var valueHost = el('div');
    var reason = textarea('修改理由（必填，会展示给审批人）', 200);

    var currentControl = null;

    function renderValueControl() {
      U.clear(valueHost);
      var fieldKey = fieldSelect.value;
      var meta = metaOf(fieldKey, cfg.kind);
      currentControl = buildValueControl(meta);

      var hintText = '请输入新的' + meta.label;
      if (cfg.current && cfg.current[fieldKey] !== undefined && cfg.current[fieldKey] !== '') {
        hintText = '当前值：' + cfg.current[fieldKey];
      }
      valueHost.appendChild(field('新的' + meta.label, currentControl, hintText));
    }

    fieldSelect.addEventListener('change', renderValueControl);

    body.appendChild(field('要修改的字段', fieldSelect));
    body.appendChild(valueHost);
    body.appendChild(field('理由', reason));

    var footer = el('div', 'row tight');
    var submit = el('button', 'primary', '提交修改申请');
    submit.type = 'button';
    submit.addEventListener('click', function () {
      var fieldKey = fieldSelect.value;
      var value = controlValue(currentControl);
      var reasonValue = controlValue(reason);

      var error = validateField(fieldKey, value, cfg.kind);
      if (error) { U.toast(error, 'warn'); return; }
      if (reasonValue.length < 2) { U.toast('请填写修改理由（不少于 2 个字符）', 'warn'); return; }

      cfg.submit(fieldKey, value, reasonValue, submit);
    });
    footer.appendChild(submit);
    footer.appendChild(el('span', 'hint', '提交后进入人工审批，审批通过才会写库，且执行前会保存旧值。'));

    renderValueControl();
    openModal(cfg.title, body, footer);
  }

  function buildValueControl(meta) {
    if (meta.control === 'enum') return selectOf(meta.options || [], false);
    if (meta.control === 'textarea') return textarea('请输入新的' + meta.label, meta.max || 200);
    if (meta.control === 'date') return input('date', null, null);
    if (meta.control === 'number') return input('number', null, '预算（元），可带两位小数');
    if (meta.control === 'phone') return input('text', null, '11 位手机号', 11);
    return input('text', null, '请输入新的' + meta.label, meta.max || 60);
  }

  // ------------------------------------------------------------------ 提交动作（唯一出口）
  /*
   * 所有写操作都必须经过这里：只发 {tool, args, reason}，由服务端决定是执行、拦截还是转人工审批。
   * 前端不做任何"本地判定已通过"的乐观处理——提交后一律重新拉取 overview，以服务端状态为准。
   */
  function act(tool, args, reason, button, options) {
    options = options || {};
    if (button) button.disabled = true;

    var payload = { tool: tool, args: args };
    var reasonText = reason || options.reason;
    if (reasonText) payload.reason = reasonText;

    return API.post('/api/biz/action', payload).then(function (res) {
      var data = res.data || {};
      if (data.status === 'PENDING_APPROVAL') {
        U.toast('已提交人工审批，任务号 ' + (data.taskId || '-'), 'warn');
      } else if (data.status === 'EXECUTED') {
        U.toast(data.reply || (data.statusLabel || '已执行'), 'ok');
      } else {
        U.toast(data.reply || (data.statusLabel || '已提交'), 'warn');
      }
      closeModal();
      return loadAll();
    }).catch(function (err) {
      U.toast(err.message + (err.code ? '（' + err.code + '）' : ''), 'bad');
    }).then(function () {
      if (button) button.disabled = false;
    });
  }

  // ------------------------------------------------------------------ 前端校验（服务端才是权威）
  function validateField(fieldKey, value, kind) {
    var meta = metaOf(fieldKey, kind);
    if (!value) return '请填写' + meta.label;

    if (meta.control === 'phone' && !/^1[3-9]\d{9}$/.test(value)) return meta.label + '必须是 11 位手机号';
    if (meta.control === 'date' && !/^\d{4}-\d{2}-\d{2}$/.test(value)) return meta.label + '格式必须是 yyyy-MM-dd';
    if (meta.control === 'number' && !/^\d+(\.\d{1,2})?$/.test(value)) return meta.label + '必须是数字（可带两位小数）';
    if (meta.control === 'enum' && (meta.options || []).indexOf(value) < 0) {
      return meta.label + '只能取：' + (meta.options || []).join(' / ');
    }
    if (meta.max && value.length > meta.max) return meta.label + '长度不得超过 ' + meta.max + ' 字';

    return '';
  }

  // ------------------------------------------------------------------ 事件绑定
  function bind() {
    document.getElementById('btn-login').addEventListener('click', login);
    document.getElementById('login-pass').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') login();
    });

    document.getElementById('btn-logout').addEventListener('click', logout);

    document.getElementById('btn-refresh-tasks').addEventListener('click', function () {
      var host = U.clear(document.getElementById('task-list'));
      host.appendChild(el('div', 'empty', '加载中…'));
      loadTasks();
    });

    var keyword = document.getElementById('filter-keyword');
    keyword.addEventListener('input', function () {
      state.filters.keyword = keyword.value.trim();
      renderClientTable();
    });

    ['industry', 'level', 'status'].forEach(function (name) {
      var node = document.getElementById('filter-' + name);
      node.addEventListener('change', function () {
        state.filters[name] = node.value;
        renderClientTable();
      });
    });

    document.getElementById('btn-filter-reset').addEventListener('click', function () {
      state.filters = { keyword: '', industry: '', level: '', status: '' };
      keyword.value = '';
      document.getElementById('filter-industry').value = '';
      document.getElementById('filter-level').value = '';
      document.getElementById('filter-status').value = '';
      renderClientTable();
    });

    window.addEventListener('session-expired', function () {
      U.toast('登录状态已失效，请重新登录', 'warn');
      showLogin();
    });
  }

  bind();
  boot();
})();
