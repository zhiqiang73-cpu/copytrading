# BitgetFollow — 聪明钱追踪系统

> 项目状态：规划中 / MVP 设计阶段
> 创建日期：2026-02-27

---

## 一、项目起源

起点来自一个问题：**币安"聪明钱"排行榜上那些赚大钱的人，能不能自动追踪他们的交易？**

经过调研，结论如下：

- **币安**：跟单数据无公开 API，只能爬虫，违反 ToS，延迟大，不可行
- **OKX**：API 文档不完整，开放程度不明确
- **Bitget**：提供完整的官方 Copy Trading REST API，文档清晰，完全合规，**是最优选择**

---

## 二、为什么不直接用 Bitget 官方跟单？

Bitget 网页/App 上有一键跟单功能，但自建系统的差异化价值在于：

| 功能 | 官方跟单 | 自建系统 |
|------|----------|----------|
| 多交易员组合管理 | 独立跟各一个人 | 同时跟多人，动态分配仓位 |
| 自定义风控 | 弱 | 日亏损上限、连亏暂停、总回撤熔断 |
| 利润分成（带单员抽成） | 8-10% | 绕过（影子跟单模式） |
| 数据积累与分析 | 无 | 完整历史数据，可挖掘规律 |
| 多交易员相关性检测 | 无 | 避免同方向重复仓位 |

**注意**：利润分成的节省是次要收益，核心价值是**数据积累**和**多维度分析**。

---

## 三、MVP 目标（第一阶段）

**不下单，只分析。**

核心目标：
1. 找到 Bitget 上值得追踪的交易员（2-3 个）
2. 持续采集他们的开平仓数据
3. 计算关键指标，辅助判断是否值得真实跟单

**不在 MVP 范围内**：自动下单、账户管理、实盘执行

---

## 四、技术架构

### 4.1 数据来源

使用 Bitget 官方 API v1（Futures Copy Trading）：

| 用途 | 端点 |
|------|------|
| 搜索交易员（按昵称） | `GET /api/mix/v1/trace/traderList?traderNickName=xxx` |
| 查看交易员当前持仓 | `POST /api/mix/v1/trace/report/order/currentList` |
| 查看交易员历史订单 | `POST /api/mix/v1/trace/report/order/historyList` |
| 查看交易员统计概况 | `GET /api/mix/v1/trace/traderDetail` |

所有接口**仅需只读 API Key**，无需交易权限。

### 4.2 项目结构

```
bitgetfollow/
├── README.md               # 本文档
├── requirements.txt        # 依赖
├── .env.example            # 环境变量模板（不含真实密钥）
├── config.py               # 配置：API密钥、目标交易员、轮询间隔
├── api_client.py           # Bitget API 封装（签名、请求、限流）
├── collector.py            # 定时采集器：快照 + 事件检测
├── trade_detector.py       # 对比快照，推导开仓/平仓事件
├── analyzer.py             # 指标计算引擎
├── reporter.py             # 日报/周报生成（可选）
├── main.py                 # 入口
└── data/
    └── tracker.db          # SQLite 数据库
```

### 4.3 数据库设计

**traders 表** — 交易员档案
```sql
CREATE TABLE traders (
    trader_uid      TEXT PRIMARY KEY,
    nickname        TEXT,
    first_seen      INTEGER,    -- Unix 时间戳
    roi             REAL,
    win_rate        REAL,
    follower_count  INTEGER,
    total_trades    INTEGER,
    copy_trade_days INTEGER,
    last_updated    INTEGER
);
```

**trades 表** — 完整交易记录（开仓到平仓）
```sql
CREATE TABLE trades (
    trade_id        TEXT PRIMARY KEY,   -- tracking_no
    trader_uid      TEXT,
    symbol          TEXT,
    direction       TEXT,               -- long / short
    leverage        INTEGER,
    open_price      REAL,
    open_time       INTEGER,
    close_price     REAL,
    close_time      INTEGER,
    hold_duration   INTEGER,            -- 持仓时长（秒）
    pnl_pct         REAL,               -- 盈亏百分比（不含手续费）
    margin_amount   REAL,
    is_win          INTEGER,            -- 1=盈利 0=亏损
    FOREIGN KEY (trader_uid) REFERENCES traders(trader_uid)
);
```

**snapshots 表** — 持仓快照（用于检测事件）
```sql
CREATE TABLE snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_uid      TEXT,
    timestamp       INTEGER,
    tracking_no     TEXT,
    symbol          TEXT,
    hold_side       TEXT,
    leverage        INTEGER,
    open_price      REAL,
    open_time       INTEGER,
    open_amount     REAL,
    tp_price        REAL,
    sl_price        REAL
);
```

---

## 五、核心工作流

### 5.1 初始化（首次运行）
```
1. 输入交易员昵称 → traderList API 搜索 → 拿到 traderUid
2. 拉取历史订单（最近 90 天）→ 写入 trades 表
3. 基于历史数据计算初始指标，输出分析报告
```

### 5.2 持续采集循环（每 5 秒）
```
对每个被追踪的交易员：
1. 调 currentList → 获取当前持仓快照
2. 和上次快照对比：
   - 新出现的 trackingNo → 记录开仓（时间、价格、方向）
   - 消失的 trackingNo  → 调 historyList 查平仓数据 → 写入 trades 表
3. 存储本次快照（覆盖旧快照，只保留最新一条）
```

### 5.3 指标计算（每小时）
```
基于 trades 表计算并更新：
- 胜率、平均盈亏比
- 平均持仓时间
- 交易频率
- 夏普比率
- 最大回撤
- 连亏/连胜最大值
```

---

## 六、要计算的分析指标

| 指标 | 计算公式 | 判断标准（参考） |
|------|----------|-----------------|
| **胜率** | 盈利笔数 / 总笔数 | > 55% 算不错 |
| **平均盈亏比** | 平均盈利 / 平均亏损 | > 1.5 较好 |
| **交易频率** | 总笔数 / 追踪天数 | 视风格而定 |
| **平均持仓时长** | close_time - open_time 均值 | 判断是短线还是波段 |
| **夏普比率** | mean(daily_pnl) / std(daily_pnl) × √365 | > 1.5 优秀，> 1 可接受 |
| **最大回撤** | 累积收益曲线最大跌幅 | < 20% 较安全 |
| **Calmar 比率** | 年化收益 / 最大回撤 | > 2 优秀 |
| **连续亏损最大次数** | 连续 is_win=0 的最大计数 | < 5 较稳 |
| **偏好交易品种** | 各 symbol 占比 | 了解专长范围 |
| **杠杆使用习惯** | 平均杠杆 / 最大杠杆 | 风险偏好参考 |

---

## 七、交易员筛选标准（初步）

**第一步：硬性过滤**
- 跟单天数 > 60 天（排除新手）
- 历史交易笔数 > 50 笔（样本量足够）
- 最大回撤 < 30%

**第二步：质量排序**
- 夏普比率 > 1.0
- 胜率 × 平均盈亏比 > 0.7（期望值为正）
- Calmar 比率 > 1.5

**第三步：风格匹配**
- 优先选择持仓时长 > 2 小时的（减少滑点影响）
- 优先选择交易对集中（专注 BTC/ETH）的

---

## 八、后续阶段规划（MVP 之后）

以下内容**不在当前 MVP 范围内**，留待数据积累后决策：

- **阶段二**：影子跟单执行层（用自己的 API 独立下单）
- **阶段三**：多交易员组合管理（仓位分配算法）
- **阶段四**：风险过滤层（R3000 信号 / 聪明钱共识作为门控条件）
- **阶段五**：自适应权重（根据近期表现动态调整各交易员仓位占比）

---

## 九、准备工作

在开始写代码之前需要：

1. **注册 Bitget 账号**（如果没有）
2. **创建只读 API Key**：
   - 访问 https://www.bitget.com/account/newapi
   - 权限：仅勾选 **Read-Only**
   - 建议绑定固定 IP
   - 记录：`API Key` / `Secret Key` / `Passphrase`
3. **确认目标交易员**：
   - 在 Bitget Copy Trading 页面找到要追踪的人
   - 记录他们的昵称（可以通过 API 搜索）

---

## 十、已知风险与限制

| 风险 | 说明 | 缓解方案 |
|------|------|----------|
| API 限流 | currentList 10次/秒 | 5秒轮询 + 指数退避 |
| 漏检平仓 | 程序中断期间的交易丢失 | 启动时对比历史订单补全 |
| 交易员数据隐私设置 | 部分交易员可能关闭持仓展示 | 采集时过滤，记录状态 |
| Bitget API 变更 | 历史上 v1 端点有调整 | 关注官方更新日志 |
| 样本量不足 | 新追踪的交易员历史数据少 | 先用历史订单初始化，再实时采集 |

---

*文档版本：v0.1 — 初始规划*
*下一步：准备好 Bitget API Key 后开始编写代码*
