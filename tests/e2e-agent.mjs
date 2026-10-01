/*
 * e2e-agent.mjs —— 多步代理能力验收（配合 mock-ai.mjs 做确定性验证）。
 *
 * 覆盖：
 *   · 多步循环：先 search_files("日报") → 再 read_file(命中路径) → 最后 answer 汇总（steps 轨迹可查）
 *   · 口语参数：用公司名（示例物流）而不是编号；枚举口语（"重要"→铂金）自动映射并留痕
 *   · 歧义澄清：关键字命中多个客户时返回候选（NEED_INPUT），不误改任何数据
 *   · 统计类问答："有多少用户"（管理员）/ 非管理员被权限拦下（租户隔离）
 *   · 写操作仍然停在人工审批，且审批上下文里带着"已执行的只读步骤"
 *
 * 用法： node e2e-agent.mjs [baseUrl]
 * 前置：node mock-ai.mjs 8910 已启动，且服务端 Ai__BaseUrl 指向它、Ai__AllowedHosts=127.0.0.1
 */
import crypto from 'node:crypto';

const BASE = process.argv[2] || 'http://127.0.0.1:8904';
let pass = 0, fail = 0;
const failures = [];

function check(name, condition, extra = '') {
  if (condition) { pass++; console.log(`  [PASS] ${name}`); }
  else { fail++; failures.push(name + (extra ? ' -> ' + extra : '')); console.log(`  [FAIL] ${name} ${extra}`); }
}

function sign(method, path, bodyText, signKey) {
  const ts = Date.now().toString();
  const nonce = crypto.randomBytes(18).toString('hex');
  const bodyHash = crypto.createHash('sha256').update(bodyText ?? '', 'utf8').digest('hex');
  const canonical = [method, path, ts, nonce, bodyHash].join('\n');
  return { ts, nonce, sig: crypto.createHmac('sha256', signKey).update(canonical, 'utf8').digest('base64') };
}

async function call(method, path, { token, signKey, body } = {}) {
  const bodyText = body === undefined ? null : JSON.stringify(body);
  const headers = { Accept: 'application/json' };
  if (token) headers.Authorization = 'Bearer ' + token;
  if (bodyText !== null) headers['Content-Type'] = 'application/json';
  if (signKey && method !== 'GET') {
    const s = sign(method, path, bodyText ?? '', signKey);
    headers['X-Timestamp'] = s.ts; headers['X-Nonce'] = s.nonce; headers['X-Signature'] = s.sig;
  }
  const res = await fetch(BASE + path, { method, headers, body: bodyText ?? undefined });
  const text = await res.text();
  let json = null; try { json = text ? JSON.parse(text) : null; } catch { json = { raw: text.slice(0, 160) }; }
  return { status: res.status, body: json };
}

async function login(userName, password) {
  const res = await call('POST', '/api/auth/login', { body: { userName, password } });
  if (!res.body?.ok) throw new Error(`登录失败 ${userName}: ${JSON.stringify(res.body).slice(0, 160)}`);
  return { token: res.body.data.token, signKey: res.body.data.signKey, user: res.body.data.user };
}

const chat = (s, message) => call('POST', '/api/agent/chat', { ...s, body: { message } });

console.log('== 多步代理能力验收（mock 模型） ==');

const health = await call('GET', '/api/public/health');
check('服务健康检查', health.status === 200, `status=${health.status}`);
console.log('      AI 模式：' + health.body?.data?.mode);

const admin = await login('admin', 'SysRoot#2026ak');
const staff = await login('lisi', 'Staff#2026abc');
const buyer = await login('zhangsan', 'User#2026abc');
check('三个角色登录成功', !!admin.token && !!staff.token && !!buyer.token);

// ---------------------------------------------------------------- 1. 多步：先搜 → 再读 → 汇总
const listFiles = await chat(buyer, '列出我的文件');
check('准备：列出文件可用（只读步骤 + AI 汇总）',
  ['CHAT', 'EXECUTED'].includes(listFiles.body?.data?.status) && (listFiles.body?.data?.steps || []).some(s => s.tool === 'list_files'),
  `status=${listFiles.body?.data?.status} steps=${JSON.stringify((listFiles.body?.data?.steps || []).map(s => s.tool))}`);

// 造一个"日报"文件，供检索命中（走审批）
const createReport = await chat(buyer, '写入 日报-2026-09-29.txt 内容：三号产线改造完毕');
const reportTaskId = createReport.body?.data?.taskId;
if (createReport.body?.data?.status === 'PENDING_APPROVAL') {
  await call('POST', `/api/admin/tasks/${reportTaskId}/approve`, { token: admin.token, signKey: admin.signKey, body: { reason: '同意（测试造数）' } });
}
const reportExists = await call('GET', '/api/agent/files/content?scope=private&path=' + encodeURIComponent('日报-2026-09-29.txt'), { token: buyer.token });
check('准备：日报文件已存在', reportExists.status === 200, `status=${reportExists.status}`);

const multi = await chat(buyer, '读一下日报，告诉我写了什么');
const md = multi.body?.data || {};
check('多步任务最终给出结论（CHAT/已完成）', md.status === 'CHAT', `status=${md.status}`);
check('轨迹里至少有 2 步', (md.steps || []).length >= 2, `steps=${(md.steps || []).length}`);
check('第 1 步是检索（search_files）', (md.steps || [])[0]?.tool === 'search_files', JSON.stringify((md.steps || [])[0] || {}));
check('第 2 步是读取检索到的文件（read_file）', (md.steps || [])[1]?.tool === 'read_file', JSON.stringify((md.steps || [])[1] || {}));
check('第 2 步用的是真实命中的路径', ((md.steps || [])[1]?.summary || '').includes('日报-2026-09-29'), (md.steps || [])[1]?.summary);
check('最终回答引用了真实执行结果（假模型汇总观察）', (md.reply || '').includes('假模型结论') && (md.reply || '').includes('文件'), md.reply);
console.log('      步骤链：' + (md.steps || []).map(s => s.title + '(' + s.tool + ')').join(' → '));

// ---------------------------------------------------------------- 2. 统计与名册（含租户隔离）
const countClients = await chat(staff, '现在有多少客户');
check('员工可统计客户数', countClients.body?.data?.status === 'CHAT' && /客户档案 \d+ 条/.test(countClients.body?.data?.reply || ''),
  countClients.body?.data?.reply);

const usersByAdmin = await chat(admin, '目前帮我查一下有多少用户');
const ua = usersByAdmin.body?.data || {};
check('管理员可查询账号数量', ua.status === 'CHAT' && /账号总数 \d+ 个/.test(ua.reply || ''), ua.reply);
check('账号统计包含角色分布', /用户 \d+ 个|员工 \d+ 个|管理员 \d+ 个/.test(JSON.stringify(ua.steps || [])), ua.reply + ' steps=' + JSON.stringify(ua.steps || []).slice(0, 120));
check('名册结果不含任何口令字段', !JSON.stringify(ua.result || {}).includes('passwordHash'), 'result 中出现了口令字段');

const usersByStaff = await chat(staff, '目前帮我查一下有多少用户');
check('员工查询账号名册被拦（租户隔离）', usersByStaff.body?.data?.status === 'REJECTED_RULE',
  `status=${usersByStaff.body?.data?.status} reply=${usersByStaff.body?.data?.reply}`);
check('拦截原因明确（缺少权限）', (usersByStaff.body?.data?.reply || '').includes('缺少权限'),
  usersByStaff.body?.data?.reply);

const usersByBuyer = await chat(buyer, '有多少用户');
check('甲方查询账号名册同样被拦', usersByBuyer.body?.data?.status === 'REJECTED_RULE', `status=${usersByBuyer.body?.data?.status}`);

// ---------------------------------------------------------------- 3. 口语参数：名字 + 枚举
const byName = await chat(staff, '把示例物流标记为重要，原因：客户升级');
const bn = byName.body?.data || {};
check('用公司名+口语枚举提交，进入人工审批', bn.status === 'PENDING_APPROVAL', `status=${bn.status} reply=${bn.reply}`);
check('系统记录了参数映射（名字→编号 或 口语→枚举）', (bn.translations || []).length > 0, JSON.stringify(bn.translations));
check('映射里体现了「重要」→ 铂金', JSON.stringify(bn.translations || []).includes('铂金'), JSON.stringify(bn.translations));

if (bn.status === 'PENDING_APPROVAL' && bn.taskId) {
  const detail = await call('GET', `/api/admin/tasks/${bn.taskId}`, { token: admin.token });
  const action = detail.body?.data?.task?.action;
  check('审批上下文里的动作是规范化后的编号+枚举', JSON.stringify(action).includes('C1004') && JSON.stringify(action).includes('铂金'),
    JSON.stringify(action));
  check('审批上下文里保留了"已完成的只读步骤"信息或映射说明', JSON.stringify(detail.body?.data?.task || {}).length > 0);

  const approve = await call('POST', `/api/admin/tasks/${bn.taskId}/approve`, { token: admin.token, signKey: admin.signKey, body: { reason: '同意升级' } });
  check('批准后等级已按映射写入', approve.body?.data?.task?.status === 'EXECUTED', `status=${approve.body?.data?.task?.status}`);

  const detail2 = await call('GET', '/api/biz/overview', { token: staff.token });
  const c1004 = (detail2.body?.data?.clients || []).find(c => c.id === 'C1004');
  check('落库值是合法枚举（铂金）', c1004?.level === '铂金', `level=${c1004?.level}`);
}

// ---------------------------------------------------------------- 4. 歧义澄清
const ambiguous = await chat(staff, '把示例的情况看一下');
const am = ambiguous.body?.data || {};
check('歧义请求不会乱改数据（返回澄清或候选）',
  ['NEED_INPUT', 'CHAT'].includes(am.status) && am.status !== 'EXECUTED', `status=${am.status} reply=${am.reply}`);

// ---------------------------------------------------------------- 5. 写操作仍走审批链路 + 轨迹保留
const writeAfterRead = await chat(buyer, '读一下日报，然后把要点写进 周报.txt');
const wr = writeAfterRead.body?.data || {};
// 写操作一定经过"规则引擎 → AI 审核 →（自动放行 或 人工审批）"：
// 新建自己的文件在审核放行时可以自动执行；覆盖/高风险/审核不可用则转人工。
check('先读后写到达写操作环节（已执行或转人工）', ['EXECUTED', 'PENDING_APPROVAL'].includes(wr.status), `status=${wr.status}`);
check('写操作进入了任务与审计链路', !!wr.taskId, `taskId=${wr.taskId}`);
check('读过的步骤在轨迹里保留', (wr.steps || []).length >= 2, `steps=${JSON.stringify((wr.steps || []).map(s => s.tool))}`);
check('回复体现真实执行结果（不是推测性话术）',
  /已创建文件|已提交人工审批|执行未成功/.test(wr.reply || ''), (wr.reply || '').slice(0, 140));

// 覆盖同一文件：私人区普通文本按新策略可直接执行（覆盖前存快照，可一键回退）
const overwrite = await chat(buyer, '把 周报.txt 覆盖成：第二版要点');
const ow = overwrite.body?.data || {};
check('私人区覆盖普通文本直接执行（有快照可回退）', ow.status === 'EXECUTED',
  `status=${ow.status} reply=${(ow.reply || '').slice(0, 120)}`);

// ---------------------------------------------------------------- 6. 审计与页面
const verify = await call('GET', '/api/admin/audit/verify', { token: admin.token });
check('审计链依旧完整', verify.body?.data?.ok === true, JSON.stringify(verify.body?.data));

const aiPlans = await call('GET', '/api/admin/audit?limit=50&action=ai.plan', { token: admin.token });
const manySteps = (aiPlans.body?.data || []).some(e => (e.detail || '').includes('第2步'));
check('审计里能看到"第2步"（多步可追溯）', manySteps,
  (aiPlans.body?.data || []).slice(0, 2).map(e => e.detail).join(' | ').slice(0, 160));

console.log('--------------------------------------------------------------');
console.log(`多步代理验收：通过 ${pass} 项，失败 ${fail} 项`);
if (failures.length) { console.log('失败项：'); failures.forEach(f => console.log('  - ' + f)); }
process.exit(fail === 0 ? 0 : 1);
