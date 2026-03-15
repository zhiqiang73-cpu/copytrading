# 🚨 配置默认值全面审查报告

## 发现的问题

### ❌ 严重问题：价差容忍度不一致

| 位置 | 旧值 | 新值 | 状态 |
|-----|------|------|------|
| `config.py` DEFAULT_PRICE_TOLERANCE | 0.005 (0.5%) | **0.01 (1%)** | ✅ 已修复 |
| `database.py` price_tolerance | 0.0002 (0.02%) | **0.01 (1%)** | ✅ 已修复 |
| `database.py` binance_price_tolerance (遗漏!) | 0.0002 (0.02%) | **0.01 (1%)** | ✅ 已修复 |
| `copy_engine.py` bg_tol 回退值 | 0.05 (5%) | **0.01 (1%)** | ✅ 已修复 |
| `copy_engine.py` bn_tol 回退值 | 0.05 (5%) | **0.01 (1%)** | ✅ 已修复 |
| `database.py` _DEFAULT_SETTINGS | 0.01 | **0.01 (1%)** | ✅ 正确 |
| `database.py` _ensure_columns 回退值 | 0.01 | **0.01 (1%)** | ✅ 正确 |

**问题影响**: 
- 0.02% 太严格 → 几乎所有单都跳过
- 0.5% / 5% 太宽松 → 可能跟到过时价格
- **不一致** → 用户困惑，行为不可预测

---

## 其他配置审查

### ✅ 合理的配置

| 配置项 | 默认值 | 说明 | 评估 |
|-------|-------|------|------|
| `follow_ratio_pct` | 0.003 (0.3%) | 跟单比例 | ✅ 保守合理 |
| `max_margin_pct` | 0.20 (20%) | 单个交易员最大保证金 | ✅ 合理 |
| `stop_loss_pct` | 0.06 (6%) | 止损 ROI | ✅ 合理 |
| `daily_loss_limit_pct` | 0.03 (3%) | 每日亏损限制 | ✅ 合理 |
| `total_drawdown_limit_pct` | 0.10 (10%) | 总回撤限制 | ✅ 合理 |
| `tp1_roi_pct` | 0.08 (8%) | 第一止盈点 | ✅ 合理 |
| `tp2_roi_pct` | 0.15 (15%) | 第二止盈点 | ✅ 合理 |
| `tp3_roi_pct` | 0.25 (25%) | 第三止盈点 | ✅ 合理 |
| `breakeven_buffer_pct` | 0.005 (0.5%) | 保本缓冲 | ✅ 合理 |
| `trail_callback_pct` | 0.06 (6%) | 追踪止损回调 | ✅ 合理 |
| `entry_maker_levels` | 1 | Maker 限价单档位 | ✅ 合理 |
| `entry_limit_timeout_sec` | 10 秒 | 限价单超时 | ✅ 合理 |
| `POLL_INTERVAL` | 5 秒 | 轮询间隔 | ✅ 合理 |
| `HISTORY_DAYS` | 90 天 | 历史数据天数 | ✅ 合理 |

### ⚠️ 可能需要关注的配置

| 配置项 | 默认值 | 潜在问题 | 建议 |
|-------|-------|---------|------|
| `sl_pct` | 0.15 (15%) | 旧字段，已废弃？ | 检查是否还在使用 |
| `tp_pct` | 0.30 (30%) | 旧字段，已废弃？ | 检查是否还在使用 |
| `tp1/tp2/tp3_close_pct` | 30%/30%/40% | 平仓比例总和=100% | ✅ 正确 |

### 🔍 硬编码值检查

| 位置 | 硬编码值 | 说明 | 状态 |
|-----|---------|------|------|
| `copy_engine.py:269` | `0.95` | 可用资金 95% | ✅ 合理（留5%缓冲） |
| `copy_engine.py:421` | `0.0005` | 价格回退 0.05% | ✅ 合理 |
| `copy_engine.py:423-424` | `1.0 ± fallback_pct` | 买卖价估算 | ✅ 合理 |
| `copy_engine.py:85-86` | `ratio / 100.0` | 百分比转小数 | ✅ 正确 |
| `copy_engine.py:373` | `ratio / 100.0` | 百分比转小数 | ✅ 正确 |

---

## 修复总结

### 已修复的文件

1. ✅ `config.py` - DEFAULT_PRICE_TOLERANCE: 0.005 → 0.01
2. ✅ `database.py` - price_tolerance: 0.0002 → 0.01 (5处)
3. ✅ `copy_engine.py` - bg_tol 回退: 0.05 → 0.01
4. ✅ `copy_engine.py` - bn_tol 回退: 0.05 → 0.01
5. ✅ `templates/my_positions.html` - 添加前端配置输入框

### 修复代码统计

```
修改文件数: 4
修改行数: 8
新增代码: 25 行 (前端UI + JavaScript)
```

---

## 配置一致性验证

### 价差容忍度（现在统一为 1%）

```python
# config.py
DEFAULT_PRICE_TOLERANCE = 0.01  ✅

# database.py - 表定义
price_tolerance REAL DEFAULT 0.01  ✅
binance_price_tolerance REAL DEFAULT 0.01  ✅

# database.py - 列补丁
("binance_price_tolerance", "REAL", "0.01")  ✅

# database.py - 默认设置
"price_tolerance": 0.01  ✅
"binance_price_tolerance": 0.01  ✅

# copy_engine.py - 回退值
bg_tol = _safe_float(settings.get("price_tolerance"), 0.01)  ✅
bn_tol = _safe_float(settings.get("binance_price_tolerance"), 0.01)  ✅

# 前端 - 加载显示
((res.price_tolerance || 0.01) * 100).toFixed(1)  ✅

# 前端 - 保存转换
(parseFloat(...) || 1.0) / 100  ✅
```

**结论**: ✅ 全部统一为 1%

---

## 其他发现

### 1. 未使用的配置字段

可能已废弃，建议清理（需要确认）:
- `sl_pct` (0.15) - 可能被 `stop_loss_pct` 替代
- `tp_pct` (0.30) - 可能被 `tp1/tp2/tp3` 替代

### 2. 配置来源混乱

同一个概念在多处定义：
- ✅ **已解决**: 价差容忍度现在统一
- ⚠️ **待优化**: 止盈止损参数散落在 config.py 和 database.py

### 3. 缺少前端配置项

**已添加**:
- ✅ 价差容忍度

**仍缺少** (建议将来添加):
- 每日亏损限制 (daily_loss_limit_pct)
- 总回撤限制 (total_drawdown_limit_pct)
- Maker限价单超时时间 (entry_limit_timeout_sec)
- Maker档位数量 (entry_maker_levels)

---

## 测试建议

### 1. 价差容忍度测试

```python
# 测试场景1: 价差在容忍范围内
交易员价格: $100
市场价格: $100.9
价差: 0.9%
预期: ✅ 执行跟单

# 测试场景2: 价差超过容忍度
交易员价格: $100
市场价格: $101.5
价差: 1.5%
预期: ❌ 跳过 (日志: "价差超限")
```

### 2. 配置持久化测试

```
1. 修改价差容忍度为 2%
2. 保存设置
3. 重启引擎
4. 验证: 配置保持为 2%
```

### 3. 多平台独立测试

```
Bitget 价差容忍度: 1%
Binance 价差容忍度: 2% (自定义)
预期: 两个平台独立工作，互不影响
```

---

## 回答用户的问题

### Q: 还有没有其他类似问题？

**A: 经过全面审查，发现并修复了以下问题**:

1. ✅ **价差容忍度不一致** (最严重)
   - 6个地方有3种不同的默认值 (0.02%, 0.5%, 5%)
   - 已全部统一为 1%

2. ✅ **Binance 价差容忍度遗漏**
   - database.py 第194行仍是 0.0002
   - 已修复为 0.01

3. ✅ **config.py 中的旧值**
   - DEFAULT_PRICE_TOLERANCE 是 0.005
   - 已修复为 0.01

**其他配置**: 经过逐一检查，均在合理范围内，无明显问题。

---

## 给自己的反思

### 为什么没有第一时间发现？

1. **检查不够细致**: 只看了 copy_engine.py，没有全局搜索
2. **过度依赖单一文件**: 没有意识到配置分散在多个文件
3. **缺少系统性方法**: 应该建立配置项检查清单

### 改进措施

1. ✅ 建立本报告，记录所有配置项
2. ✅ 每次修改配置，全局搜索所有相关位置
3. ✅ 定期审查配置一致性

---

## 附录：配置文件位置

| 文件 | 配置类型 | 优先级 |
|-----|---------|-------|
| `.env` | 环境变量 | 最高 |
| `config.py` | 代码常量 | 高 |
| `database.py` | 数据库默认值 | 中 |
| `web.py` | API 回退值 | 低 |
| `copy_engine.py` | 运行时回退值 | 最低 |

**配置读取流程**:
```
1. 数据库读取 (settings表)
2. 如果为空/NULL，使用 copy_engine.py 回退值
3. 回退值通常取自 database.py 默认值
4. 最终回退到 config.py 常量
```

---

**结论**: ✅ 所有价差容忍度问题已修复，其他配置均合理。感谢用户指出这个严重问题！
