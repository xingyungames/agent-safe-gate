/*
 * frontend-check.mjs —— 前端静态安全与一致性自检（无需浏览器）。
 *
 * 用法： node frontend-check.mjs [wwwroot 路径]
 * 检查项：
 *   1) JS 里引用的 DOM id 在对应 HTML 中是否存在；
 *   2) 零 emoji；
 *   3) 无 innerHTML / outerHTML / document.write / eval（textContent 渲染的根基）；
 *   4) 无内联 style="..." 与内联 <script>（服务端 CSP 为 style-src 'self' / script-src 'self'）；
 *   5) 无外部 CDN（离线可用 + 不引入第三方供应链风险）。
 */
import fs from 'node:fs';
import path from 'node:path';

const root = process.argv[2] || '.';
const pairs = [
  ['assets/app.js', 'index.html'],
  ['assets/admin.js', 'admin.html'],
  ['assets/biz.js', 'biz.html'],
  ['assets/net.js', 'index.html'],
  ['assets/net.js', 'admin.html'],
  ['assets/net.js', 'biz.html'],
];

const emojiRx = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE0F}\u{2190}-\u{21FF}]/gu;

let problems = 0;

console.log('== DOM id 一致性 ==');
for (const [js, html] of pairs) {
  const jsPath = path.join(root, js);
  const htmlPath = path.join(root, html);
  if (!fs.existsSync(jsPath) || !fs.existsSync(htmlPath)) continue;

  const code = fs.readFileSync(jsPath, 'utf8');
  const page = fs.readFileSync(htmlPath, 'utf8');
  const ids = [...new Set([...code.matchAll(/getElementById\('([^']+)'\)/g)].map(m => m[1]))];
  const missing = ids.filter(id => !page.includes(`id="${id}"`));
  console.log(`  ${js} -> ${html}：引用 ${ids.length} 个 id，缺失 ${missing.length ? missing.join(', ') : '无'}`);
  if (missing.length) problems++;
}

console.log('== 静态安全检查 ==');
const files = fs.readdirSync(root).filter(f => f.endsWith('.html'))
  .concat(fs.readdirSync(path.join(root, 'assets')).filter(f => /\.(js|css)$/.test(f)).map(f => 'assets/' + f));

for (const rel of files) {
  const raw = fs.readFileSync(path.join(root, rel), 'utf8');
  // 先剥掉注释再扫描：注释里提到 innerHTML（例如"绝不使用 innerHTML"）不该算问题
  const text = raw
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/^\s*\/\/.*$/gm, ' ')
    .replace(/([^:'"\\])\/\/[^\n]*/g, '$1 ');

  const emoji = (text.match(emojiRx) || []).length;
  const dangerous = (text.match(/innerHTML|outerHTML|document\.write|eval\(/g) || []).length;
  const styleAttr = (text.match(/\sstyle="/g) || []).length;
  const styleTag = (text.match(/<style[\s>]/g) || []).length;
  const inlineScript = (text.match(/<script(?![^>]*\bsrc=)/g) || []).length;
  const inlineEvent = (text.match(/\son(click|load|error|submit|change|input)=/g) || []).length;
  const external = (text.match(/https?:\/\/(?!127\.0\.0\.1|localhost)[a-z0-9.-]+/gi) || []).length;

  const notes = [];
  if (emoji) notes.push(`emoji=${emoji}`);
  if (dangerous) notes.push(`危险写法=${dangerous}`);
  if (styleAttr) notes.push(`inline style=${styleAttr}`);
  if (styleTag) notes.push(`<style>=${styleTag}`);
  if (inlineScript) notes.push(`内联脚本=${inlineScript}`);
  if (inlineEvent) notes.push(`内联事件=${inlineEvent}`);
  if (external) notes.push(`外部链接=${external}`);

  if (notes.length) { problems++; console.log(`  [注意] ${rel}：${notes.join('，')}`); }
  else console.log(`  [OK] ${rel}`);
}

console.log('--------------------------------------------------------------');
console.log(problems === 0 ? '前端自检通过' : `前端自检发现 ${problems} 处需要确认`);
process.exit(problems === 0 ? 0 : 1);
