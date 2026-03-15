import requests

# 尝试多个可能的端点
endpoints = [
    ("旧端点", "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/list"),
    ("bapi v2", "https://www.binance.com/bapi/futures/v2/public/future/copy-trade/lead-portfolio/list"),
    ("bapi public", "https://www.binance.com/bapi/futures/v1/public/future/copy-trade/lead-portfolio/list"),
]

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
}

payload = {
    "pageSize": 20,
    "pageNumber": 1,
    "sortBy": "copierPnl",
    "sortType": "desc",
}

for name, url in endpoints:
    try:
        print(f"\n测试 {name}: {url}")
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"  状态码: {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            code = data.get("code")
            items = data.get("data", {}).get("data", [])
            print(f"  API Code: {code}")
            print(f"  数据条数: {len(items)}")
            if items:
                print(f"  ✓ 成功！第一条: {items[0].get('nickname')}")
                break
    except Exception as e:
        print(f"  错误: {e}")
