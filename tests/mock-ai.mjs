/*
 * mock-ai.mjs —— 用于**确定性验证多步代理循环**的假模型服务（仅测试用，不进生产）。
 *
 * 为什么需要它：真实模型不可控（每次措辞不同、还依赖外网），而"AI 是否真的先搜→再读→再汇总"
 * 这条链路必须能被反复、稳定地验证。这个服务实现了与 DeepSeek /chat/completions 兼容的接口，
 * 按"系统提示词 + 已收到的观察"来**脚本化**地决定下一步动作。
 *
 * 用法： node mock-ai.mjs [port]          默认 8910
 * 配套： 启动服务端时把 AI 指过来（自建网关需在白名单中显式放行）：
 *         $env:Ai__BaseUrl="http://127.0.0.1:8910/chat/completions"
 *         $env:Ai__AllowedHosts="127.0.0.1"
 *         $env:AISERVER_AI_KEY="sk-mock"
 */
import http from 'node:http';

const PORT = Number(process.argv[2] || 8910);
let calls = 0;

function reply(content) {
  return {
    id: 'mock-' + (++calls),
    object: 'chat.completion',
    model: 'mock-model',
    choices: [{ index: 0, message: { role: 'assistant', content: JSON.stringify(content) }, finish_reason: 'stop' }],
    usage: { prompt_tokens: 100, completion_tokens: 20, total_tokens: 120 },
  };
}

/** 从"观察块"里抽文件路径（只扫 observations/clarify，避免被历史对话里的路径干扰） */
function firstFoundPath(userContent) {
  const blocks = [...userContent.matchAll(/<(observation|clarify)[\s\S]*?<\/\1>/g)].map(m => m[0]).join('\n');
  const m = blocks.match(/"path"\s*:\s*"([^"]+)"/i);
  return m ? m[1] : '';
}

/** 从观察块里抽出各步的"结果："摘要（模拟真实模型会引用工具结果作答） */
function observationSummaries(userContent) {
  return [...userContent.matchAll(/结果：([^\n<]+)/g)].map(m => m[1].trim());
}

function decide(systemContent, userContent) {
  // 1) 审核 AI（物理隔离的那个）：一律放行，方便聚焦测试主链路
  if (systemContent.includes('操作安全审核员')) {
    return { verdict: 'allow', risk: 'LOW', category: 'mock', reason: '假模型：模拟审核放行' };
  }

  // 2) 收尾总结调用（系统提示里要求汇总 observations）
  if (systemContent.includes('已经替你执行完若干只读步骤')) {
    const summaries = observationSummaries(userContent);
    return { reply: '假模型结论：' + (summaries.length ? summaries.join('；') : '已完成。') };
  }

  const observed = [...userContent.matchAll(/tool="([^"]+)"/g)].map(m => m[1]);
  const hasSearchObservation = observed.includes('search_files');
  const hasReadObservation = observed.includes('read_file');
  const hasWriteObservation = observed.includes('write_file');
  const hasAnyObservation = observed.length > 0;
  const question = (userContent.match(/<user_input>([\s\S]*?)<\/user_input>/) || [, ''])[1];
  const summaries = observationSummaries(userContent);

  // ---------- P0 验收专用场景（按特征短语触发，便于确定性测试） ----------
  if (/重复调用测试/.test(question)) {
    // 无论看到什么观察都继续调同一个工具：用来验证"无进展收敛"是否生效
    return { intent: '统计客户数量', tool: 'count_records', args: { dataset: 'customers' }, reply: '再统计一次。' };
  }
  if (/多动作测试/.test(question)) {
    if (!hasAnyObservation) return { intent: '列出个人工作区文件', tool: 'list_files', args: { scope: 'private' }, reply: '先列文件。' };
    if (!observed.includes('read_files') && !observed.includes('read_file'))
      return { intent: '读取列出的文件', tool: 'read_files',
        args: { paths: /日报\.txt/.test(userContent) ? '日报.txt' : (firstFoundPath(userContent) || '日报.txt'), scope: 'private' },
        reply: '再读内容。' };
    if (!observed.includes('write_file'))
      return { intent: '追加访问时间戳', tool: 'write_file',
        args: { path: '日报.txt', content: '[2026-10-01 09:00] 访问记录', mode: 'append', reason: '追加访问时间戳' }, reply: '追加时间戳。' };
    return { intent: '汇总', tool: 'answer', args: {}, reply: '假模型结论：三件事都做完了（列出、读取、追加）。' };
  }
  if (/写脚本测试/.test(question)) {
    return { intent: '写入含下载执行的脚本文件', tool: 'write_file',
      args: { path: 'daily.bat', content: 'powershell -enc SQBFAFgA; Invoke-WebRequest http://169.254.169.254/latest/meta-data -OutFile a.txt', mode: 'create', reason: '用户要求生成脚本' },
      reply: '提交脚本写入。' };
  }
  if (/改名测试/.test(question)) {
    return { intent: '把日报改名为工作日志', tool: 'rename_file',
      args: { path: '日报.txt', new_name: '工作日志.txt', reason: '改名' }, reply: '提交改名。' };
  }
  if (/改后缀测试/.test(question)) {
    return { intent: '把日报改成可执行后缀', tool: 'rename_file',
      args: { path: '日报.txt', new_name: '日报.bat', reason: '改成 bat' }, reply: '提交改名（改后缀）。' };
  }
  if (/批量删除测试/.test(question)) {
    return { intent: '清空个人工作区全部文件', tool: 'delete_file', args: { path: '', reason: '清空工作区' }, reply: '尝试批量删除。' };
  }
  if (/我的区域测试/.test(question)) {
    return { intent: '查看我可用哪些区域', tool: 'my_workspace', args: {}, reply: '查一下可用区域。' };
  }
  if (/撤回测试/.test(question)) {
    return { intent: '撤回我最新一条待审批申请', tool: 'cancel_my_task', args: { reason: '不想做了' }, reply: '尝试撤回。' };
  }
  if (/使用情况测试/.test(question)) {
    return { intent: '汇总各账号近期使用情况', tool: 'a_activity_summary', args: { days: '7' }, reply: '拉取使用情况汇总。' };
  }
  if (/自行拒绝测试/.test(question)) {
    return { intent: '用户要求做一个需要审批的操作', tool: 'answer', args: {}, reply: '这个操作看起来有风险，我不做。' };
  }

  // 3) 已经看过检索结果 → 读第一个命中的文件（"多步"的第二跳）
  if (hasSearchObservation && !hasReadObservation && /日[报志]|文件|内容/.test(question)) {
    const path = firstFoundPath(userContent);

    // 用户还要求"写进/写入"时，读完再写（第三跳）
    if (path) {
      return { intent: '读取检索到的文件', tool: 'read_file', args: { path }, reply: '读一下命中的那个文件。' };
    }
    return { intent: '没有找到文件', tool: 'answer', args: {}, reply: '没有找到匹配的文件。' };
  }

  // 4) 已经读过文件 → 若用户要求写文件则提交写操作（会停在人工审批），否则收尾
  if (hasReadObservation && !hasWriteObservation && /写进|写入|写到|保存|覆盖/.test(question)) {
    const target = (question.match(/([^\s，。]*\.(?:txt|md))/i) || [, '周报.txt'])[1];
    const overwrite = /覆盖/.test(question);
    return { intent: '把要点写入文件', tool: 'write_file',
      args: {
        path: target,
        content: (question.match(/(?:覆盖成|内容)[:：]\s*([^\n]+)/) || [, '要点：' + (summaries[summaries.length - 1] || '已完成')])[1],
        mode: overwrite ? 'overwrite' : 'create',
        reason: '假模型：汇总日报要点',
      },
      reply: '把要点写进文件。' };
  }

  if (hasReadObservation || hasWriteObservation) {
    return { intent: '汇总已读内容', tool: 'answer', args: {}, reply: '假模型结论：' + (summaries.join('；') || '已完成。') };
  }

  // 5) 已经拿到了统计/名册类结果 → 直接收尾（不要重复调用同一个工具）
  if (hasAnyObservation) {
    return { intent: '汇总结果', tool: 'answer', args: {}, reply: '假模型结论：' + (summaries.join('；') || '已完成。') };
  }

  // 6) 第一步：按用户问句决定动作
  const wantsRead = /读|看|检索|查一下|搜|打开/.test(question);

  // "读…然后写…" 这类复合请求：先读（后续步骤会自己接上写入）
  if (wantsRead && /日[报志]|文件|记录/.test(question)) {
    const keyword = (/日[报志]/.test(question) ? '日报' : (question.match(/([\u4e00-\u9fa5A-Za-z0-9_\-]{2,10})\.(?:txt|md)/) || [, '文件'])[1]);
    return { intent: '检索文件', tool: 'search_files', args: { keyword, limit: '10' }, reply: '先找一下文件。' };
  }
  // 覆盖写：走 write_file 的 overwrite 模式（服务端会因此强制人工审批）
  if (/覆盖|改写|替换/.test(question) && /\.(txt|md)/i.test(question)) {
    const path = (question.match(/([^\s，。]*\.(?:txt|md))/i) || [, 'note.txt'])[1];
    const content = (question.match(/(?:覆盖成|内容)[:：]\s*([^\n]+)/) || [, '假模型覆盖后的内容'])[1];
    return { intent: '覆盖写入文件', tool: 'write_file', args: { path, content, mode: 'overwrite', reason: '假模型：覆盖写入' }, reply: '提交覆盖写入。' };
  }
  if (/写入|写进|保存|创建/.test(question) && /\.(txt|md)/i.test(question)) {
    const path = (question.match(/([^\s，。]*\.(?:txt|md))/i) || [, 'note.txt'])[1];
    const content = (question.match(/内容[:：]\s*([^\n]+)/) || [, '假模型写入的内容'])[1];
    return { intent: '写入文件', tool: 'write_file', args: { path, content, mode: 'create' }, reply: '提交写入。' };
  }
  if (/日[报志]/.test(question)) {
    return { intent: '检索日报文件', tool: 'search_files', args: { keyword: '日报', limit: '10' }, reply: '先找一下日报文件。' };
  }
  if (/有多少用户|用户数|账号数|多少账号|有哪些员工|有哪些账号/.test(question)) {
    return { intent: '统计账号数量', tool: 'list_users', args: { limit: '100' }, reply: '正在统计账号。' };
  }
  if (/有多少客户|客户数|客户有多少/.test(question)) {
    return { intent: '统计客户数量', tool: 'count_records', args: { dataset: 'customers' }, reply: '正在统计客户。' };
  }
  if (/(列出|有哪些|看看).*(文件|目录)/.test(question)) {
    return { intent: '列出工作区文件', tool: 'list_files', args: { scope: 'private' }, reply: '列出文件。' };
  }
  if (/示例物流.*(重要|等级)/.test(question)) {
    return { intent: '把示例物流标记为重要', tool: 'update_client_profile',
      args: { client: '示例物流', field: 'level', value: '重要', reason: '假模型：客户升级' }, reply: '已提交等级调整。' };
  }
  if (/示例/.test(question) && /情况|怎么样|看看/.test(question)) {
    return { intent: '查看示例相关客户', tool: 'find_client', args: { keyword: '示例' }, reply: '先按关键字找一下。' };
  }

  return { intent: '无法确定', tool: 'answer', args: {}, reply: '假模型：我无法确定你的意图。' };
}

const server = http.createServer((req, res) => {
  if (req.method !== 'POST') {
    res.writeHead(405).end('POST only');
    return;
  }

  let body = '';
  req.on('data', c => { body += c; });
  req.on('end', () => {
    try {
      const parsed = JSON.parse(body);
      const messages = parsed.messages || [];
      const systemContent = messages.find(m => m.role === 'system')?.content || '';
      const userContent = messages.find(m => m.role === 'user')?.content || '';
      const payload = reply(decide(systemContent, userContent));

      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify(payload));
    } catch (e) {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: { message: String(e) } }));
    }
  });
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`mock-ai 已启动：http://127.0.0.1:${PORT}/chat/completions（仅测试用）`);
});
