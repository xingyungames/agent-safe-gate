//报销单写入口验收，配合 mock-feishu.mjs
//用法：node e2e-invoice.mjs [服务地址] [假飞书地址]
import crypto from 'node:crypto';
const BASE = process.argv[2] || 'http://127.0.0.1:8990';
const MOCK = process.argv[3] || 'http://127.0.0.1:8991';
let pass = 0, fail = 0; const bad = [];
//断言并累计通过/失败
function check(n, c, e = '') { if (c) { pass++; console.log(`  [PASS] ${n}`); } else { fail++; bad.push(n + (e ? ' -> ' + e : '')); console.log(`  [FAIL] ${n} ${e}`); } }
//生成请求签名头
function sign(m, p, b, k) { const ts = Date.now().toString(); const nonce = crypto.randomBytes(18).toString('hex'); const h = crypto.createHash('sha256').update(b ?? '', 'utf8').digest('hex'); const sig = crypto.createHmac('sha256', k).update([m, p, ts, nonce, h].join('\n'), 'utf8').digest('base64'); return { ts, nonce, sig }; }
//发请求并解析 JSON
async function call(m, p, { token, signKey, body } = {}) {
  const bt = body === undefined ? null : JSON.stringify(body);
  const h = { Accept: 'application/json' };
  if (token) h.Authorization = 'Bearer ' + token;
  if (bt !== null) h['Content-Type'] = 'application/json';
  if (signKey && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(m)) { const s = sign(m, p, bt ?? '', signKey); h['X-Timestamp'] = s.ts; h['X-Nonce'] = s.nonce; h['X-Signature'] = s.sig; }
  const r = await fetch(BASE + p, { method: m, headers: h, body: bt === null ? undefined : bt, redirect: 'manual' });
  const t = await r.text(); let j = null; try { j = t ? JSON.parse(t) : null; } catch { j = { raw: t.slice(0, 200) }; }
  return { status: r.status, body: j };
}
//登录取 token 与签名密钥
async function login(u, p) { const r = await call('POST', '/api/auth/login', { body: { userName: u, password: p } }); return r.status === 200 ? { token: r.body.data.token, signKey: r.body.data.signKey, user: r.body.data.user } : { error: r.body }; }

const emp = await login('employee', 'Staff#2026abc');
const fin = await login('finance', 'Money#2026abc');
const adm = await login('admin', 'SysRoot#2026ak');
check('三个角色登录', !!emp.token && !!fin.token && !!adm.token);

//契约元数据与权限下发
const meta = await call('GET', '/api/invoice/meta', { token: emp.token });
check('契约元数据可读', meta.status === 200 && Array.isArray(meta.body?.data?.types), `status=${meta.status}`);
check('员工看不到全部单据', meta.body?.data?.canSeeAll === false);
check('部门/城市枚举下发', (meta.body?.data?.departments || []).length === 11 && (meta.body?.data?.cities || []).length === 37);
check('财务可审核、员工不可', (await call('GET', '/api/invoice/meta', { token: fin.token })).body?.data?.canReview === true && meta.body?.data?.canReview === false);

//非法与无票提交被拒
const bad1 = await call('POST', '/api/invoice/submit', { token: emp.token, signKey: emp.signKey, body: { department: '火星部', rtype: '招待费', amount: 100, reason: '测试非法部门', hasInvoice: '有票' } });
check('非法部门被拒（枚举白名单）', bad1.status === 400, `status=${bad1.status} msg=${bad1.body?.message}`);

const bad2 = await call('POST', '/api/invoice/submit', { token: emp.token, signKey: emp.signKey, body: { department: '市场部', rtype: '招待费', amount: 100, reason: '无票测试', hasInvoice: '无票' } });
check('无票提交被阻断', bad2.status === 400 && (bad2.body?.message || '').includes('无票'), `status=${bad2.status} msg=${bad2.body?.message}`);

const bad3 = await call('POST', '/api/invoice/submit', { token: emp.token, signKey: emp.signKey, body: { department: '市场部', rtype: '差旅费', amount: 100, reason: '缺城市', hasInvoice: '有票' } });
check('差旅费缺城市被拒', bad3.status === 400 && (bad3.body?.message || '').includes('城市'), `msg=${bad3.body?.message}`);

//合法提交并落库
const ok1 = await call('POST', '/api/invoice/submit', {
  token: emp.token, signKey: emp.signKey,
  body: { department: '市场部', rtype: '招待费', amount: 2500, reason: 'Q4 客户招待餐费', hasInvoice: '有票', city: '上海', invoiceDate: '2026-10-01' },
});
check('合法提交成功', ok1.status === 200 && !!ok1.body?.data?.recordId, `status=${ok1.status} msg=${ok1.body?.message} err=${ok1.body?.message}`);
const rec1 = ok1.body?.data?.recordId;
check('返回提示含合规结论', (ok1.body?.data?.message || '').includes('风险'), ok1.body?.data?.message);

//列表可见范围
const list1 = await call('GET', '/api/invoice/list', { token: emp.token });
check('员工列表只含自己的单', list1.status === 200 && list1.body?.data?.scope === 'mine' && list1.body.data.count >= 1, JSON.stringify(list1.body?.data?.count));
const row = (list1.body?.data?.rows || []).find(r => r.recordId === rec1);
check('单据字段完整（服务端盖章）', row && row.applicant === '张三' && /^E\d{4}$/.test(row.employeeId) && row.status === '待审核' && row.compliance === '风险', JSON.stringify(row));
check('合规摘要写明超标原因', (row?.riskSummary || '').includes('招待费超2000'), row?.riskSummary);

const list2 = await call('GET', '/api/invoice/list', { token: fin.token });
check('财务可看全部（scope=all）', list2.body?.data?.scope === 'all', list2.body?.data?.scope);

//审核与修改权限
const detailOther = await call('GET', '/api/invoice/detail?recordId=' + rec1, { token: (await login('employee', 'Staff#2026abc')).token });
check('本人可看自己单据详情', detailOther.status === 200, `status=${detailOther.status}`);

const reviewByEmp = await call('POST', '/api/invoice/review', { token: emp.token, signKey: emp.signKey, body: { recordId: rec1, status: '已通过', opinion: '员工越权审核' } });
check('员工无权审核（403）', reviewByEmp.status === 403, `status=${reviewByEmp.status}`);

const review1 = await call('POST', '/api/invoice/review', { token: fin.token, signKey: fin.signKey, body: { recordId: rec1, status: '审核中', opinion: '票据齐全，进入复核' } });
check('财务审核成功', review1.status === 200, `status=${review1.status} msg=${review1.body?.message}`);

const review2 = await call('POST', '/api/invoice/review', { token: fin.token, signKey: fin.signKey, body: { recordId: rec1, status: '已通过', opinion: '票据齐全，准予报销' } });
check('二次审核成功', review2.status === 200, `status=${review2.status} msg=${review2.body?.message}`);

const review3 = await call('POST', '/api/invoice/review', { token: fin.token, signKey: fin.signKey, body: { recordId: rec1, status: '已驳回', opinion: '终态后不应再改' } });
check('终态不可再改状态', review3.status === 400, `status=${review3.status} msg=${review3.body?.message}`);

const upd = await call('POST', '/api/invoice/update', { token: fin.token, signKey: fin.signKey, body: { recordId: rec1, fields: { '金额': '1800', '事由': '修正后的事由说明' } } });
check('财务修改成功', upd.status === 200, `status=${upd.status} msg=${upd.body?.message}`);

const updBad = await call('POST', '/api/invoice/update', { token: fin.token, signKey: fin.signKey, body: { recordId: rec1, fields: { '单号': 'BX-999999' } } });
check('只读字段不可改', updBad.status === 400, `status=${updBad.status} msg=${updBad.body?.message}`);

const delByFin = await call('POST', '/api/invoice/delete', { token: fin.token, signKey: fin.signKey, body: { recordId: rec1, reason: '财务越权删除测试' } });
check('财务无权删除（403）', delByFin.status === 403, `status=${delByFin.status}`);

const del = await call('POST', '/api/invoice/delete', { token: adm.token, signKey: adm.signKey, body: { recordId: rec1, reason: '重复报销，删除测试' } });
check('管理员删除成功（含快照）', del.status === 200, `status=${del.status} msg=${del.body?.message}`);

//版本记录与回退
const versions = await call('GET', '/api/admin/versions?limit=50', { token: adm.token });
const vList = versions.body?.data?.versions || versions.body?.data || [];
const delVersion = vList.find(v => v.operation === 'delete' && (v.target || '').includes(rec1));
check('删除留下了版本记录', !!delVersion, JSON.stringify(vList.slice(0, 3).map(v => ({ op: v.operation, target: v.target }))));

//按删除版本回退重建
if (delVersion) {
  const rb = await call('POST', '/api/invoice/rollback', { token: adm.token, signKey: adm.signKey, body: { versionId: delVersion.id } });
  check('按日志回退删除（重建单据）', rb.status === 200, `status=${rb.status} msg=${rb.body?.message}`);
  const list3 = await call('GET', '/api/invoice/list', { token: adm.token });
  const rebuilt = (list3.body?.data?.rows || []).find(r => r.reason === '修正后的事由说明');
  check('重建后的单据字段与快照一致', !!rebuilt, JSON.stringify((list3.body?.data?.rows || []).map(r => r.reason)));
  const rebuiltFields = await mockRow(rb.body?.data?.recordId || '');
  check('重建后的金额是数字类型', typeof rebuiltFields?.['金额'] === 'number', JSON.stringify(rebuiltFields?.['金额']));
  const rbAgain = await call('POST', '/api/invoice/rollback', { token: adm.token, signKey: adm.signKey, body: { versionId: delVersion.id } });
  check('同一版本不可重复回退', rbAgain.status === 400, `status=${rbAgain.status} msg=${rbAgain.body?.message}`);
}

const submitVersion = vList.find(v => v.operation === 'submit');
//回退新建的单据
if (submitVersion) {
  const rb2 = await call('POST', '/api/invoice/rollback', { token: adm.token, signKey: adm.signKey, body: { versionId: submitVersion.id } });
  check('回退新建（删除该单据）', rb2.status === 200, `status=${rb2.status} msg=${rb2.body?.message}`);
}

const finRb = await call('POST', '/api/invoice/rollback', { token: fin.token, signKey: fin.signKey, body: { versionId: submitVersion?.id || 'v_x' } });
check('财务无权回退（403）', finRb.status === 403, `status=${finRb.status}`);

//认证边界与身份盖章
const anonList = await call('GET', '/api/invoice/list');
check('未登录读列表被拒（401）', anonList.status === 401, `status=${anonList.status}`);

const unsignedSubmit = await call('POST', '/api/invoice/submit', {
  token: emp.token,
  body: { department: '市场部', rtype: '交通费', amount: 50, reason: '未签名测试', hasInvoice: '有票' },
});
check('未签名写请求被拒（401）', unsignedSubmit.status === 401, `status=${unsignedSubmit.status} code=${unsignedSubmit.body?.code}`);

const forged = await call('POST', '/api/invoice/submit', {
  token: emp.token, signKey: emp.signKey,
  body: {
    applicant: '李四', employeeId: 'E0002', department: '市场部', rtype: '交通费',
    amount: 66, reason: '伪造身份测试', hasInvoice: '有票',
  },
});
check('伪造申请人与工号也照常受理（服务端覆盖）', forged.status === 200, `status=${forged.status} msg=${forged.body?.message}`);
const forgedRow = (await call('GET', '/api/invoice/list', { token: emp.token }))
  .body?.data?.rows?.find(r => r.reason === '伪造身份测试');
check('落库申请人仍是登录人本人', forgedRow?.applicant === '张三', JSON.stringify(forgedRow?.applicant));
check('落库工号仍是登录人工号', forgedRow?.employeeId === 'E0001', JSON.stringify(forgedRow?.employeeId));

//员工之间的横向越权
const second = await call('POST', '/api/auth/register', {
  token: adm.token, signKey: adm.signKey,
  body: { userName: 'employee2', password: 'Second#2026abc', displayName: '钱七', role: 'employee' },
});
check('管理员可创建第二个员工账号', second.status === 200, `status=${second.status} msg=${second.body?.message}`);

const emp2 = await login('employee2', 'Second#2026abc');
check('第二个员工可登录', !!emp2.token, JSON.stringify(emp2.error));

const listBySecond = await call('GET', '/api/invoice/list', { token: emp2.token });
check('第二个员工列表里看不到他人单据', (listBySecond.body?.data?.count ?? -1) === 0, `count=${listBySecond.body?.data?.count}`);

const detail2 = await call('GET', '/api/invoice/detail?recordId=' + forgedRow?.recordId, { token: emp2.token });
check('第二个员工读他人详情被拒（403）', detail2.status === 403, `status=${detail2.status} msg=${detail2.body?.message}`);

const reviewBySecond = await call('POST', '/api/invoice/review', {
  token: emp2.token, signKey: emp2.signKey,
  body: { recordId: forgedRow?.recordId, status: '已通过', opinion: '越权审批测试' },
});
check('员工无权审批他人单据（403）', reviewBySecond.status === 403, `status=${reviewBySecond.status}`);

//回退审核：把快照写回飞书时必须还原成真实类型
//（历史缺陷：快照里每个字段存的是 JSON 文本，回退时直接写回会变成带引号的字符串与字符串日期，
//  飞书侧报 DatetimeFieldConvFail / WrongRequestBody，管理后台看到的是一次看不懂的失败）
async function mockRow(recordId) {
  const res = await fetch(MOCK + '/__mock/state');
  const state = await res.json();
  for (const rows of Object.values(state.data?.tables || {})) {
    const hit = rows.find(r => r.record_id === recordId);
    if (hit) return hit.fields;
  }
  return null;
}

const rbTarget = await call('POST', '/api/invoice/submit', {
  token: emp.token, signKey: emp.signKey,
  body: {
    department: '研发部', rtype: '交通费', amount: 777, reason: '回退审核用单',
    hasInvoice: '有票', invoiceDate: '2026-10-05',
  },
});
check('准备：提交一张用于审核回退的单', rbTarget.status === 200 && !!rbTarget.body?.data?.recordId, `status=${rbTarget.status}`);
const rbRecord = rbTarget.body?.data?.recordId;

const rbReview = await call('POST', '/api/invoice/review', {
  token: fin.token, signKey: fin.signKey,
  body: { recordId: rbRecord, status: '已通过', opinion: '先通过再回退' },
});
check('准备：财务审核通过（产生 review 版本）', rbReview.status === 200, `status=${rbReview.status} msg=${rbReview.body?.message}`);

const rbList = await call('GET', '/api/admin/versions?limit=100', { token: adm.token });
const rbVersions = rbList.body?.data?.versions || rbList.body?.data || [];
const reviewVersion = rbVersions.find(v => v.operation === 'review' && (v.target || '').includes(rbRecord));
check('准备：找到该单的 review 版本', !!reviewVersion, JSON.stringify(rbVersions.slice(0, 5).map(v => ({ op: v.operation, target: v.target }))));

if (reviewVersion) {
  const rbReviewBack = await call('POST', '/api/invoice/rollback', {
    token: adm.token, signKey: adm.signKey, body: { versionId: reviewVersion.id },
  });
  check('回退审核成功（不再报飞书字段类型错误）', rbReviewBack.status === 200,
    `status=${rbReviewBack.status} msg=${rbReviewBack.body?.message}`);

  const after = await mockRow(rbRecord);
  check('回退后能读到飞书行', !!after, JSON.stringify(after));
  check('回退写回的文本没有多余引号', after?.事由 === '回退审核用单', JSON.stringify(after?.事由));
  check('回退写回的数字仍是数字类型', typeof after?.金额 === 'number' && after.金额 === 777, JSON.stringify(after?.金额));
  check('回退写回的日期列是毫秒数字（飞书只认这个）', typeof after?.发票日期 === 'number' && after.发票日期 > 0, JSON.stringify(after?.发票日期));
  check('回退后状态回到审核前（待审核）', after?.状态 === '待审核', JSON.stringify(after?.状态));
  check('回退后审批意见已清空', (after?.审批意见 ?? '') === '', JSON.stringify(after?.审批意见));

  const rbReviewAgain = await call('POST', '/api/invoice/rollback', {
    token: adm.token, signKey: adm.signKey, body: { versionId: reviewVersion.id },
  });
  check('同一 review 版本不可重复回退', rbReviewAgain.status === 400, `status=${rbReviewAgain.status}`);
}

const verify = await call('GET', '/api/admin/audit/verify', { token: adm.token });
check('审计哈希链完整', verify.body?.data?.ok === true, JSON.stringify(verify.body?.data));

console.log('--------------------------------------------------------------');
console.log(`报销单验收：通过 ${pass} 项，失败 ${fail} 项`);
if (bad.length) console.log('失败项：\n  ' + bad.join('\n  '));
process.exit(fail === 0 ? 0 : 1);
