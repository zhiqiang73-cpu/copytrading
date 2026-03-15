import requests

url = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/list"
headers = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/json",
}
payload = {
    "pageSize": 20,
    "pageNumber": 1,
    "sortBy": "copierPnl",
    "sortType": "desc",
}

try:
    r = requests.post(url, json=payload, headers=headers, timeout=10)
    print(f"HTTP Status: {r.status_code}")
    data = r.json()
    print(f"API Code: {data.get('code')}")
    print(f"API Message: {data.get('msg')}")
    items = data.get("data", {}).get("data", [])
    print(f"Items count: {len(items)}")
    if items:
        print(f"First item: {items[0].get('nickname')} (PID: {items[0].get('portfolioId')})")
except Exception as e:
    print(f"Error: {e}")
