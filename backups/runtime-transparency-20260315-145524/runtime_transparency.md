# Runtime Transparency Report

- Generated: `2026-03-15 14:55:24`
- Profile: `live`
- Engine running: `True`
- Diagnostics overall: `pass`
- Enabled traders: `6`
- Source open positions: `14`
- Local open positions: `2`
- Recent orders: `200`
- Recent order status counts: `{"failed": 109, "filled": 34, "skipped": 57}`

## Attention

- Btc星辰: History analytics unavailable: no such column: hold_duration_sec
- DeepSeek量化-杨勇娇: Source has open ETHUSDT long qty=1.0000, but no local open position was found.
- DeepSeek量化-杨勇娇: Source has open BNBUSDT long qty=0.3000, but no local open position was found.
- DeepSeek量化-杨勇娇: Latest copy order is skipped: [Live Binance Signal] source close ignored: no remaining local position (opened=0.00000000, closed=0.00000000)
- DeepSeek量化-杨勇娇: History analytics unavailable: no such column: hold_duration_sec
- Sky株式会社: Source has open BTCUSDT long qty=1.5040, but no local open position was found.
- Sky株式会社: History analytics unavailable: no such column: hold_duration_sec
- 东方龙财经: History analytics unavailable: no such column: hold_duration_sec
- 八零二三: Source has open BUSDT long qty=14000.0000, but no local open position was found.
- 八零二三: Source has open RESOLVUSDT short qty=60000.0000, but no local open position was found.
- 八零二三: Source has open RESOLVUSDT long qty=100000.0000, but no local open position was found.
- 八零二三: Source has open RIVERUSDT short qty=1100.0000, but no local open position was found.
- 八零二三: Source has open RIVERUSDT long qty=1100.0000, but no local open position was found.
- 八零二三: Source has open SIRENUSDT short qty=4000.0000, but no local open position was found.
- 八零二三: Source has open UAIUSDT short qty=6000.0000, but no local open position was found.
- 八零二三: Source has open COLLECTUSDT short qty=50000.0000, but no local open position was found.
- 八零二三: Source has open COLLECTUSDT long qty=50000.0000, but no local open position was found.
- 八零二三: History analytics unavailable: no such column: hold_duration_sec
- 鎏渊: Source has open ETHUSDT long qty=300.0000, but no local open position was found.
- 鎏渊: History analytics unavailable: no such column: hold_duration_sec
- Recent order RIVERUSDT long open on live_binance is skipped: [跳过] price drift too large src=21.8000 now=22.8510 dev=4.82% [比例跟随] ratio=50.0000% src=10900.0000 target=5450.0000 [保...
- Recent order RIVERUSDT long open on live_bitget is skipped: [跳过] price drift too large src=21.8000 now=22.8340 dev=4.74% [比例跟随] ratio=50.0000% src=10900.0000 target=5450.0000 [保...
- Recent order ETHUSDT long close on live_binance is skipped: [Live Binance Signal] source close ignored: no remaining local position (opened=0.00000000, closed=0.00000000)
- Recent order ETHUSDT long close on live_bitget is skipped: [Live Bitget Signal] source close ignored: no remaining local position (opened=0.00000000, closed=0.00000000)
- Recent order ETHUSDT long open on live_binance is skipped: [跳过] price drift too large src=2070.0000 now=2112.3900 dev=2.05% [比例跟随] ratio=50.0000% src=621000.0000 target=310500....
- Recent order ETHUSDT long open on live_bitget is skipped: [跳过] Bitget 最小开仓金额不足 need=21.1241 cap=8.3333 [比例跟随] ratio=50.0000% src=621000.0000 target=310500.0000 [保证金裁剪] src=310...
- Recent order RIVERUSDT long open on live_binance is skipped: [跳过] price drift too large src=21.8000 now=22.3170 dev=2.37% [比例跟随] ratio=50.0000% src=141700.0000 target=70850.0000 ...
- Recent order RIVERUSDT long open on live_bitget is skipped: [跳过] Bitget 最小开仓金额不足 need=22.3080 cap=8.3333 [比例跟随] ratio=50.0000% src=141700.0000 target=70850.0000 [保证金裁剪] src=7085...

## Traders

### Btc星辰

- Trader UID: `4751838302089254401`
- Poll status: `pass`; age=`5.6` sec; new orders=`0`
- Warmup: `existing_history`; sync-open: `-`; pending=`False`
- Latest source event: `ETHUSDT close_short @ 2026-03-13 23:00:27`
- Latest copy order: `ETHUSDT close filled @ 2026-03-13 23:02:31`
- Dynamic sizing: score=`0` clip=`0.0%` reverse=`0.0%` hold=`0` sec median_margin=`0.0`
- Issues:
  - History analytics unavailable: no such column: hold_duration_sec
- Position comparison:
  - no active source/local positions
- Recent copy orders:
  - 2026-03-13 23:02:31 | live_binance | ETHUSDT short close filled | note=[Live Binance Signal] Close
  - 2026-03-13 21:33:28 | live_binance | ETHUSDT short open filled | note=[Live Binance Signal] src=502744.3431 [比例跟随] ratio=50.0000% src=502744.3431 target=251372.1716 [保证金裁剪] src=251372.171...
  - 2026-03-13 21:32:54 | live_bitget | ETHUSDT short open skipped | note=[跳过] Bitget 最小开仓金额不足 need=21.8917 cap=12.5000 [比例跟随] ratio=50.0000% src=502744.3431 target=251372.1716 [保证金裁剪] src=25...
  - 2026-03-13 21:32:34 | live_binance | ETHUSDT short open filled | note=[Live Binance Signal] src=1048206.4096 [比例跟随] ratio=50.0000% src=1048206.4096 target=524103.2048 [保证金裁剪] src=524103.2...
  - 2026-03-13 21:32:04 | live_bitget | ETHUSDT short open skipped | note=[跳过] Bitget 最小开仓金额不足 need=21.8750 cap=12.5000 [比例跟随] ratio=50.0000% src=1048206.4096 target=524103.2048 [保证金裁剪] src=5...
  - 2026-03-13 21:28:59 | live_binance | ETHUSDT short open filled | note=[Live Binance Signal] src=695303.9377 [比例跟随] ratio=50.0000% src=695303.9377 target=347651.9689 [保证金裁剪] src=347651.968...

### DeepSeek量化-杨勇娇

- Trader UID: `4906010685108267264`
- Poll status: `pass`; age=`4.8` sec; new orders=`0`
- Warmup: `existing_history`; sync-open: `-`; pending=`False`
- Latest source event: `ETHUSDT close_long @ 2026-03-15 13:18:01`
- Latest copy order: `ETHUSDT close skipped @ 2026-03-15 13:43:34`
- Dynamic sizing: score=`0` clip=`0.0%` reverse=`0.0%` hold=`0` sec median_margin=`0.0`
- Issues:
  - Source has open ETHUSDT long qty=1.0000, but no local open position was found.
  - Source has open BNBUSDT long qty=0.3000, but no local open position was found.
  - Latest copy order is skipped: [Live Binance Signal] source close ignored: no remaining local position (opened=0.00000000, closed=0.00000000)
  - History analytics unavailable: no such column: hold_duration_sec
- Position comparison:
  - BNBUSDT long: source qty=0.3000 margin=196.42 | local none
  - ETHUSDT long: source qty=1.0000 margin=2096.28 | local none
- Recent copy orders:
  - 2026-03-15 13:43:34 | live_binance | ETHUSDT long close skipped | note=[Live Binance Signal] source close ignored: no remaining local position (opened=0.00000000, closed=0.00000000)
  - 2026-03-15 13:43:34 | live_bitget | ETHUSDT long close skipped | note=[Live Bitget Signal] source close ignored: no remaining local position (opened=0.00000000, closed=0.00000000)
  - 2026-03-13 13:01:35 | live_binance | XRPUSDT long close filled | note=[Manual Flat Sync] User confirmed exchange positions were manually flattened at 2026-03-13 13:01:35 +0800; clearing s...
  - 2026-03-13 13:01:35 | live_bitget | XRPUSDT long close filled | note=[Manual Flat Sync] User confirmed exchange positions were manually flattened at 2026-03-13 13:01:35 +0800; clearing s...
  - 2026-03-13 13:01:35 | live_binance | XRPUSDT short close filled | note=[Manual Flat Sync] User confirmed exchange positions were manually flattened at 2026-03-13 13:01:35 +0800; clearing s...
  - 2026-03-10 22:39:25 | live_binance | XRPUSDT long open filled | note=[Live Binance Signal] src=399.8915 [比例跟随] ratio=40.0000% src=399.8915 target=159.9566 [保证金裁剪] src=159.9566 cap=15.000...

### Sky株式会社

- Trader UID: `4532994172262753536`
- Poll status: `pass`; age=`6.4` sec; new orders=`0`
- Warmup: `existing_history`; sync-open: `-`; pending=`False`
- Latest source event: `BTCUSDT close_long @ 2026-03-13 23:40:31`
- Latest copy order: `- - - @ -`
- Dynamic sizing: score=`0` clip=`0.0%` reverse=`0.0%` hold=`0` sec median_margin=`0.0`
- Issues:
  - Source has open BTCUSDT long qty=1.5040, but no local open position was found.
  - History analytics unavailable: no such column: hold_duration_sec
- Position comparison:
  - BTCUSDT long: source qty=1.5040 margin=101619.59 | local none
- Recent copy orders:
  - no recent local copy orders

### 东方龙财经

- Trader UID: `4934518209294885889`
- Poll status: `pass`; age=`3.2` sec; new orders=`0`
- Warmup: `existing_history`; sync-open: `-`; pending=`False`
- Latest source event: `ETHUSDT close_short @ 2026-03-15 04:41:21`
- Latest copy order: `- - - @ -`
- Dynamic sizing: score=`0` clip=`0.0%` reverse=`0.0%` hold=`0` sec median_margin=`0.0`
- Issues:
  - History analytics unavailable: no such column: hold_duration_sec
- Position comparison:
  - no active source/local positions
- Recent copy orders:
  - no recent local copy orders

### 八零二三

- Trader UID: `4917375922961797377`
- Poll status: `pass`; age=`4.0` sec; new orders=`0`
- Warmup: `existing_history`; sync-open: `-`; pending=`False`
- Latest source event: `COLLECTUSDT open_long @ 2026-03-14 03:13:31`
- Latest copy order: `- - - @ -`
- Dynamic sizing: score=`0` clip=`0.0%` reverse=`0.0%` hold=`0` sec median_margin=`0.0`
- Issues:
  - Source has open BUSDT long qty=14000.0000, but no local open position was found.
  - Source has open RESOLVUSDT short qty=60000.0000, but no local open position was found.
  - Source has open RESOLVUSDT long qty=100000.0000, but no local open position was found.
  - Source has open RIVERUSDT short qty=1100.0000, but no local open position was found.
  - Source has open RIVERUSDT long qty=1100.0000, but no local open position was found.
  - Source has open SIRENUSDT short qty=4000.0000, but no local open position was found.
  - Source has open UAIUSDT short qty=6000.0000, but no local open position was found.
  - Source has open COLLECTUSDT short qty=50000.0000, but no local open position was found.
  - Source has open COLLECTUSDT long qty=50000.0000, but no local open position was found.
  - History analytics unavailable: no such column: hold_duration_sec
- Position comparison:
  - BUSDT long: source qty=14000.0000 margin=2499.00 | local none
  - COLLECTUSDT long: source qty=50000.0000 margin=3102.50 | local none
  - COLLECTUSDT short: source qty=50000.0000 margin=3521.34 | local none
  - RESOLVUSDT long: source qty=100000.0000 margin=11715.00 | local none
  - RESOLVUSDT short: source qty=60000.0000 margin=7561.63 | local none
  - RIVERUSDT long: source qty=1100.0000 margin=15476.50 | local none
  - RIVERUSDT short: source qty=1100.0000 margin=16838.01 | local none
  - SIRENUSDT short: source qty=4000.0000 margin=2262.36 | local none
  - UAIUSDT short: source qty=6000.0000 margin=2051.05 | local none
- Recent copy orders:
  - no recent local copy orders

### 鎏渊

- Trader UID: `4937267073165751809`
- Poll status: `pass`; age=`2.4` sec; new orders=`0`
- Warmup: `existing_history`; sync-open: `-`; pending=`False`
- Latest source event: `RIVERUSDT open_long @ 2026-03-15 11:53:02`
- Latest copy order: `RIVERUSDT open filled @ 2026-03-15 14:02:36`
- Dynamic sizing: score=`0` clip=`0.0%` reverse=`0.0%` hold=`0` sec median_margin=`0.0`
- Issues:
  - Source has open ETHUSDT long qty=300.0000, but no local open position was found.
  - History analytics unavailable: no such column: hold_duration_sec
- Position comparison:
  - ETHUSDT long: source qty=300.0000 margin=625700.00 | local none
  - RIVERUSDT long: source qty=6500.0000 margin=117811.60 | local live_binance qty=1.0000 margin=22.73
- Recent copy orders:
  - 2026-03-15 14:02:36 | live_binance | RIVERUSDT long open filled | note=[Live Binance Signal] src=141700.0000 [比例跟随] ratio=50.0000% src=141700.0000 target=70850.0000 [保证金裁剪] src=70850.0000 ...
  - 2026-03-15 13:43:40 | live_binance | RIVERUSDT long open skipped | note=[跳过] price drift too large src=21.8000 now=22.8510 dev=4.82% [比例跟随] ratio=50.0000% src=10900.0000 target=5450.0000 [保...
  - 2026-03-15 13:43:38 | live_bitget | RIVERUSDT long open skipped | note=[跳过] price drift too large src=21.8000 now=22.8340 dev=4.74% [比例跟随] ratio=50.0000% src=10900.0000 target=5450.0000 [保...
  - 2026-03-15 13:24:44 | live_binance | ETHUSDT long open skipped | note=[跳过] price drift too large src=2070.0000 now=2112.3900 dev=2.05% [比例跟随] ratio=50.0000% src=621000.0000 target=310500....
  - 2026-03-15 13:24:44 | live_bitget | ETHUSDT long open skipped | note=[跳过] Bitget 最小开仓金额不足 need=21.1241 cap=8.3333 [比例跟随] ratio=50.0000% src=621000.0000 target=310500.0000 [保证金裁剪] src=310...
  - 2026-03-15 13:24:42 | live_binance | RIVERUSDT long open skipped | note=[跳过] price drift too large src=21.8000 now=22.3170 dev=2.37% [比例跟随] ratio=50.0000% src=141700.0000 target=70850.0000 ...

## Recent Warnings

- `bitgetfollow.log` 07:08:08  ERROR    binance_scraper  Binance GET https://www.binance.com/bapi/futures/v1/friendly/future/copy 失败: HTTPSConnectionPool(host='www.binance.com', port=443): Read timed out. (read timeout=10)
- `bitgetfollow.log` 07:08:08  WARNING  binance_scraper  币安 API 获取失败，使用基础信息: 491333754772
- `bitgetfollow.log` 07:24:13  WARNING  web  检测到网页已关闭（60秒无心跳），即将自动退出进程
- `web.log` 22:39:24  ERROR    binance_scanner  扫描流程异常: BrowserType.launch: Executable doesn't exist at /Users/wangzhiqiang/Library/Caches/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell
- `web.log` 22:43:30  WARNING  copy_engine  [币安平仓信号] 本地未发现 ETHUSDT SHORT 的剩余持仓 (pid=47786476)，跳过
- `web.log` 22:43:32  WARNING  copy_engine  [币安价差过大暂缓] SCRTUSDT LONG 信号价=0.0818 现价=0.0794 偏差=2.96%
- `web.log` 22:43:32  WARNING  copy_engine  [币安平仓信号] 本地未发现 SCRTUSDT LONG 的剩余持仓 (pid=49133375)，跳过
- `web.log` 22:43:32  WARNING  copy_engine  [币安价差过大暂缓] KITEUSDT LONG 信号价=0.2591 现价=0.2715 偏差=4.79%
- `web.log` 22:43:32  WARNING  copy_engine  [币安平仓信号] 本地未发现 KITEUSDT LONG 的剩余持仓 (pid=49133375)，跳过

## Recent Lifecycle Lines

- `bitgetfollow.log` /Users/wangzhiqiang/Library/Python/3.9/lib/python/site-packages/urllib3/__init__.py:35: NotOpenSSLWarning: urllib3 v2 only supports OpenSSL 1.1.1+, currently the 'ssl' module is compiled with 'LibreSSL 2.8.3'. See: https://github.com/urllib3/urllib3/issues/3020
- `bitgetfollow.log` warnings.warn(
- `bitgetfollow.log` 18:29:45  INFO     werkzeug  [31m[1mWARNING: This is a development server. Do not use it in a production deployment. Use a production WSGI server instead.[0m
- `bitgetfollow.log` ModuleNotFoundError: No module named 'flask'
- `bitgetfollow.log` 07:08:08  ERROR    binance_scraper  Binance GET https://www.binance.com/bapi/futures/v1/friendly/future/copy 失败: HTTPSConnectionPool(host='www.binance.com', port=443): Read timed out. (read timeout=10)
- `bitgetfollow.log` 07:08:08  WARNING  binance_scraper  币安 API 获取失败，使用基础信息: 491333754772
- `bitgetfollow.log` 07:24:13  WARNING  web  检测到网页已关闭（60秒无心跳），即将自动退出进程
- `web.log` /Users/wangzhiqiang/Library/Python/3.9/lib/python/site-packages/urllib3/__init__.py:35: NotOpenSSLWarning: urllib3 v2 only supports OpenSSL 1.1.1+, currently the 'ssl' module is compiled with 'LibreSSL 2.8.3'. See: https://github.com/urllib3/urllib3/issues/3020
- `web.log` warnings.warn(
- `web.log` 22:38:54  INFO     werkzeug  [31m[1mWARNING: This is a development server. Do not use it in a production deployment. Use a production WSGI server instead.[0m
- `web.log` 22:39:24  ERROR    binance_scanner  扫描流程异常: BrowserType.launch: Executable doesn't exist at /Users/wangzhiqiang/Library/Caches/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell
- `web.log` raise rewrite_error(error, f"{parsed_st['apiName']}: {error}") from None
- `web.log` playwright._impl._errors.Error: BrowserType.launch: Executable doesn't exist at /Users/wangzhiqiang/Library/Caches/ms-playwright/chromium_headless_shell-1208/chrome-headless-shell-mac-arm64/chrome-headless-shell
- `web.log` 22:43:30  WARNING  copy_engine  [币安平仓信号] 本地未发现 ETHUSDT SHORT 的剩余持仓 (pid=47786476)，跳过
- `web.log` 22:43:32  WARNING  copy_engine  [币安价差过大暂缓] SCRTUSDT LONG 信号价=0.0818 现价=0.0794 偏差=2.96%
- `web.log` 22:43:32  WARNING  copy_engine  [币安平仓信号] 本地未发现 SCRTUSDT LONG 的剩余持仓 (pid=49133375)，跳过
- `web.log` 22:43:32  WARNING  copy_engine  [币安价差过大暂缓] KITEUSDT LONG 信号价=0.2591 现价=0.2715 偏差=4.79%
- `web.log` 22:43:32  WARNING  copy_engine  [币安平仓信号] 本地未发现 KITEUSDT LONG 的剩余持仓 (pid=49133375)，跳过
