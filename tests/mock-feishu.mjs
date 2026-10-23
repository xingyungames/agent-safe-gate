// 假飞书开放平台：账号镜像链路的离线验收
// 用法 node mock-feishu.mjs [port] [--fail]
import http from 'node:http';

const argPort = process.argv.slice(2).find(a => /^\d+$/.test(a));
const PORT = Number(argPort || process.env.MOCK_FEISHU_PORT || 8991);
let failMode = process.argv.includes('--fail') || process.env.MOCK_FEISHU_FAIL === '1';
let failFirst = 0;
// 每页最多几行 0 表示按请求值
let pageLimit = 0;

const tables = new Map();
const tableFields = new Map();
const counters = { token: 0, create: 0, update: 0, list: 0, get: 0, remove: 0, fields: 0, rejected: 0, broken: 0 };
const requests = [];

// 报销单主表的默认列
const DEFAULT_FIELDS = ['单号', '申请人', '工号', '部门', '报销类型', '金额', '事由', '票据状态', '城市',
  '发票日期', '记录提交时间', '合规结果', '风险摘要', '状态', '审核人', '审批意见', '审批时间', '发票信息', '发票附件'];

// 真实飞书的类型硬要求：金额(Currency)必须是数字，日期(DateTime)必须是毫秒数字
function validateFieldTypes(fields) {
  if (!fields || typeof fields !== 'object') return null;
  for (const [name, value] of Object.entries(fields)) {
    if (name === '金额' && typeof value !== 'number') return { code: 1254001, msg: 'WrongRequestBody' };
    if (['发票日期', '记录提交时间', '审批时间'].includes(name) && typeof value !== 'number') {
      return { code: 1254064, msg: 'DatetimeFieldConvFail' };
    }
  }
  return null;
}

// 取或建某张表
function tableOf(tableId) {
  if (!tables.has(tableId)) tables.set(tableId, new Map());
  return tables.get(tableId);
}

// 取或建表的字段名列表
function fieldListOf(tableId) {
  if (!tableFields.has(tableId)) tableFields.set(tableId, [...DEFAULT_FIELDS]);
  return tableFields.get(tableId);
}

// 生成随机记录 id
function nextRecordId() {
  return 'rec' + Math.random().toString(16).slice(2, 14).padEnd(12, '0');
}

// 自动编号 BX- 加 6 位
let billSeq = 0;
function nextBillNo() {
  billSeq += 1;
  return 'BX-' + String(billSeq).padStart(6, '0');
}

// 返回 JSON 响应
function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
  });
  res.end(body);
}

// 返回飞书成功体
function feishuOk(res, data) {
  sendJson(res, 200, { code: 0, msg: 'success', data });
}

// 返回飞书错误体
function feishuError(res, code, msg) {
  sendJson(res, 200, { code, msg });
}

// 读取并解析请求体
function readBody(req) {
  return new Promise(resolve => {
    let raw = '';
    req.on('data', c => { raw += c; });
    req.on('end', () => {
      if (!raw) return resolve({});
      try { resolve(JSON.parse(raw)); } catch { resolve({}); }
    });
  });
}

// 汇总当前状态
function dumpState() {
  const tableDump = {};
  for (const [tableId, records] of tables) {
    tableDump[tableId] = [...records.entries()].map(([recordId, fields]) => ({ record_id: recordId, fields }));
  }
  return { fail: failMode, failFirst, pageLimit, counters, tables: tableDump, requests: requests.slice(-80) };
}

// 假飞书入口 按路径分流
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const path = url.pathname;
  const headerFail = req.headers['x-mock-fail'] === '1';
  const failed = failMode || headerFail;

  if (!path.startsWith('/__mock/')) {
    requests.push({ method: req.method, path: req.url, fail: failed, at: new Date().toISOString() });
  }

  // 传输层故障 先断连若干次
  if (!path.startsWith('/__mock/') && failFirst > 0) {
    failFirst--;
    counters.broken++;
    requests.push({ method: req.method, path: req.url, dropped: true, at: new Date().toISOString() });
    req.socket.destroy();
    return;
  }

  // 控制端点
  if (path === '/__mock/state' && req.method === 'GET') {
    return feishuOk(res, dumpState());
  }

  if (path === '/__mock/records' && req.method === 'GET') {
    return feishuOk(res, dumpState());
  }

  if (path === '/__mock/mode' && req.method === 'POST') {
    const body = await readBody(req);
    const q = url.searchParams.get('fail');
    if (typeof body.fail === 'boolean') failMode = body.fail;
    else if (q !== null) failMode = q === '1' || q.toLowerCase() === 'true';
    if (typeof body.failFirst === 'number') failFirst = Math.max(0, Math.floor(body.failFirst));
    if (typeof body.pageLimit === 'number') pageLimit = Math.max(0, Math.floor(body.pageLimit));
    return feishuOk(res, { fail: failMode, failFirst, pageLimit });
  }

  if (path === '/__mock/seed' && req.method === 'POST') {
    const body = await readBody(req);
    const tableId = body.tableId || 'tbl_mock';
    const recordId = body.recordId || nextRecordId();
    tableOf(tableId).set(recordId, body.fields || {});
    return feishuOk(res, { record_id: recordId, fields: body.fields || {} });
  }

  if (path === '/__mock/reset' && req.method === 'POST') {
    tables.clear();
    tableFields.clear();
    for (const k of Object.keys(counters)) counters[k] = 0;
    requests.length = 0;
    failFirst = 0;
    return feishuOk(res, { reset: true, fail: failMode, failFirst, pageLimit });
  }

  // 换票接口
  if (path === '/open-apis/auth/v3/tenant_access_token/internal' && req.method === 'POST') {
    counters.token++;
    if (failed) return feishuError(res, 99991400, 'mock injected failure');
    return sendJson(res, 200, {
      code: 0,
      msg: 'ok',
      tenant_access_token: 't-mock-' + Math.random().toString(16).slice(2, 18),
      expire: 7200,
    });
  }

  // 字段列表接口
  const fieldMatch = path.match(/^\/open-apis\/bitable\/v1\/apps\/([^/]+)\/tables\/([^/]+)\/fields$/);
  if (fieldMatch && req.method === 'GET') {
    if (failed) return feishuError(res, 99991400, 'mock injected failure');
    const tableId = decodeURIComponent(fieldMatch[2]);
    counters.fields++;
    const names = fieldListOf(tableId);
    return feishuOk(res, {
      items: names.map((n, i) => ({ field_name: n, field_id: 'fld' + i, type: 1 })),
      total: names.length,
    });
  }

  // 记录读写接口
  const recordMatch = path.match(/^\/open-apis\/bitable\/v1\/apps\/([^/]+)\/tables\/([^/]+)\/records(?:\/([^/]+))?$/);
  if (recordMatch) {
    const tableId = decodeURIComponent(recordMatch[2]);
    const recordId = recordMatch[3] ? decodeURIComponent(recordMatch[3]) : '';

    if (failed) {
      counters.rejected++;
      if (req.method === 'POST') counters.create++;
      if (req.method === 'PUT') counters.update++;
      if (req.method === 'GET') counters.list++;
      return feishuError(res, 99991400, 'mock injected failure');
    }

    if (req.method === 'GET' && !recordId) {
      counters.list++;
      // 分页 page_token 存偏移量
      const requested = Number(url.searchParams.get('page_size') || 200);
      const effective = pageLimit > 0 ? Math.min(requested, pageLimit) : requested;
      const all = [...tableOf(tableId).entries()];
      const token = url.searchParams.get('page_token') || '';
      const start = token ? Math.max(0, Number(token) || 0) : 0;
      const items = all.slice(start, start + effective).map(([id, fields]) => ({ record_id: id, fields }));
      const next = start + effective;
      const hasMore = next < all.length;
      return feishuOk(res, {
        items,
        total: all.length,
        has_more: hasMore,
        page_token: hasMore ? String(next) : '',
      });
    }

    if (req.method === 'GET' && recordId) {
      counters.get++;
      const fields = tableOf(tableId).get(recordId);
      if (!fields) return feishuError(res, 1254043, 'record not found');
      return feishuOk(res, { record: { record_id: recordId, fields } });
    }

    const body = await readBody(req);
    const fields = body && typeof body.fields === 'object' && body.fields !== null ? body.fields : {};

    if (req.method === 'POST') {
      counters.create++;
      const bad = validateFieldTypes(fields);
      if (bad) return feishuError(res, bad.code, bad.msg);
      const id = nextRecordId();
      // 单号自动补号
      const stored = { ...fields };
      if (!stored['单号'] && fieldListOf(tableId).includes('单号')) stored['单号'] = nextBillNo();
      tableOf(tableId).set(id, stored);
      return feishuOk(res, { record: { record_id: id, fields: stored } });
    }

    if (req.method === 'PUT') {
      counters.update++;
      const bad = validateFieldTypes(fields);
      if (bad) return feishuError(res, bad.code, bad.msg);
      const existing = tableOf(tableId).get(recordId);
      if (!existing) return feishuError(res, 1254043, 'record not found');
      const merged = { ...existing, ...fields };
      tableOf(tableId).set(recordId, merged);
      return feishuOk(res, { record: { record_id: recordId, fields: merged } });
    }

    if (req.method === 'DELETE') {
      counters.remove++;
      const existing = tableOf(tableId).get(recordId);
      if (!existing) return feishuError(res, 1254043, 'record not found');
      tableOf(tableId).delete(recordId);
      return feishuOk(res, { deleted: true });
    }

    return sendJson(res, 405, { code: 99991400, msg: 'method not allowed' });
  }

  return sendJson(res, 404, { code: 99991400, msg: 'mock: unknown path ' + path });
});

// 启动假飞书服务
server.listen(PORT, '127.0.0.1', () => {
  console.log(`[mock-feishu] listening on http://127.0.0.1:${PORT} (fail=${failMode})`);
});
