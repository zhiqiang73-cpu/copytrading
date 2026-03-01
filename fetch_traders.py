import os
import json
import signal
from playwright.sync_api import sync_playwright
import time

os.chdir('/Users/wangzhiqiang/My Ai/bitgetfollow')
OUTPUT_FILE = '/Users/wangzhiqiang/My Ai/bitgetfollow/data/traders_with_uid.json'

def timeout_handler(signum, frame):
    raise Exception("超时")

signal.signal(signal.SIGALRM, timeout_handler)
signal.alarm(80)  # 80秒超时

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--no-sandbox"])
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        print("打开页面...")
        page.goto("https://www.bitget.com/zh-CN/copy-trading/futures/all",
                 wait_until="domcontentloaded", timeout=60000)
        time.sleep(10)

        print("检查登录...")
        try:
            text = page.evaluate("function(){return document.body.innerText}")
            if '登录' in text[:500]:
                print("请扫码登录...")
                for i in range(60):
                    time.sleep(1)
                    try:
                        new = page.evaluate("function(){return document.body.innerText}")
                        if len(new) > 5000:
                            break
                    except: pass
        except: pass

        page.reload(wait_until="domcontentloaded")
        time.sleep(5)

        print("滚动加载...")
        for i in range(12):
            page.mouse.wheel(0, 2000)
            time.sleep(0.8)

        print("提取UID...")
        traders = page.evaluate("""() => {
            const results = [];
            const allLinks = document.querySelectorAll('a[href*="/copy-trading/trader/"]');
            allLinks.forEach(link => {
                const href = link.getAttribute('href') || '';
                const match = href.match(/\\/copy-trading\\/trader\\/([a-zA-Z0-9]{20,})/);
                if (match) {
                    const realUid = match[1];
                    let parent = link.closest('[class*="item"], [class*="card"]');
                    if (!parent) parent = link.parentElement;
                    const cardText = parent ? parent.innerText : '';
                    const roiMatch = cardText.match(/([+-]?\\d+\\.?\\d*)%/);
                    const roi = roiMatch ? roiMatch[1] + '%' : '';
                    const followMatch = cardText.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
                    const followers = followMatch ? followMatch[1] : '';
                    let name = '';
                    const nameMatch = cardText.match(/@([A-Za-z0-9_-]+)/);
                    if (nameMatch) name = '@' + nameMatch[1];
                    if (!results.find(r => r.real_uid === realUid)) {
                        results.push({ 'real_uid': realUid, 'name': name, 'roi': roi, 'followers': followers });
                    }
                }
            });
            return results;
        }""")

        print(f"\n找到 {len(traders)} 个交易员:")
        for i, t in enumerate(traders[:15], 1):
            print(f"{i:2}. {t.get('name',''):<25} {t['real_uid'][:30]}")

        with open(OUTPUT_FILE, 'w') as f:
            json.dump(traders, f, ensure_ascii=False, indent=2)
        print(f"\n已保存: {OUTPUT_FILE}")

        browser.close()
except Exception as e:
    print(f"错误: {e}")