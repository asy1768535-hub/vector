// 诊断:v5 滚轮旅程是否工作
import playwright from 'file:///C:/Users/Alice/Desktop/vectorDatabase/admin-ui/node_modules/playwright/index.js';

const { chromium } = playwright;
const EXE = 'C:/Users/Alice/AppData/Local/ms-playwright/chromium-1223/chrome-win64/chrome.exe';
const BASE = 'file:///D:/%E5%9B%BE%E7%89%87/login-preview/v5/index.html';

const browser = await chromium.launch({ executablePath: EXE });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await ctx.newPage();
page.on('pageerror', (e) => console.log('PAGE ERROR:', e.message));

await page.goto(BASE, { waitUntil: 'load' });
await page.waitForTimeout(800);

const before = await page.evaluate(() => ({
    bodyClass: document.body.className,
    gateVisible: !document.getElementById('gate').classList.contains('hide'),
}));
console.log('加载后:', JSON.stringify(before));

// 点击「进入」
await page.click('#gBtn');
await page.waitForTimeout(1200);

// 模拟滚轮向下 30 次
for (let i = 0; i < 30; i++) { await page.mouse.wheel(0, 240); await page.waitForTimeout(40); }
await page.waitForTimeout(1200);

const after = await page.evaluate(() => ({
    jpPct: document.getElementById('jpPct').textContent,
    bodyClass: document.body.className,
}));
console.log('滚轮后:', JSON.stringify(after));

await browser.close();
