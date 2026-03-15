# 📁 文档目录整理说明

**整理日期**: 2026-03-15  
**整理目的**: 改善项目文档结构，便于查找和维护

---

## 🗂️ 新的目录结构

```
bitgetfollow/
├── 📁 docs/                          # ✨ 新增 - 集中存放所有文档
│   ├── INDEX.md                      # 📚 文档索引和导航
│   ├── PROGRESS.md                   # 📊 开发进度存档（主文档）
│   ├── IMPROVEMENTS.md               # 📋 改进总览
│   ├── 📁 improvements/              # 💡 改进相关文档
│   │   ├── HEARTBEAT_UI_IMPROVEMENTS.md
│   │   ├── ENGINE_ROBUSTNESS_FIX.md
│   │   ├── URGENT_FIX_状态显示问题.md
│   │   ├── 视觉对比.md
│   │   └── 新功能快速体验.md
│   └── 📁 testing/                   # 🧪 测试相关文档
│       └── test_heartbeat_ui.md
├── 📁 tests/                         # ✨ 整理 - 所有测试脚本
│   ├── test_engine_robustness.py     # 从根目录移入
│   ├── _test_binance_api.py          # 从根目录移入
│   ├── _test_endpoints.py            # 从根目录移入
│   └── ... (其他测试文件)
├── 📁 scripts/                       # 工具脚本
├── 📁 backups/                       # 备份文件
├── 📁 templates/                     # HTML模板
├── 📁 data/                          # 数据文件
└── ... (核心Python模块)
```

---

## 📋 移动的文件清单

### 文档类 (移动到 `docs/`)

**改进文档** → `docs/improvements/`:
- ✅ `HEARTBEAT_UI_IMPROVEMENTS.md`
- ✅ `ENGINE_ROBUSTNESS_FIX.md`
- ✅ `URGENT_FIX_状态显示问题.md`
- ✅ `视觉对比.md`
- ✅ `新功能快速体验.md`

**测试文档** → `docs/testing/`:
- ✅ `test_heartbeat_ui.md`

**总览文档** → `docs/`:
- ✅ `IMPROVEMENTS.md`

### 测试脚本 (移动到 `tests/`)

- ✅ `test_engine_robustness.py` (从根目录)
- ✅ `_test_binance_api.py` (从根目录)
- ✅ `_test_endpoints.py` (从根目录)

---

## 🎯 整理的好处

### 1. **结构清晰**
- 文档集中在 `docs/` 目录
- 测试集中在 `tests/` 目录
- 根目录只保留核心代码和配置

### 2. **易于查找**
- `docs/INDEX.md` - 完整的文档导航
- `docs/PROGRESS.md` - 项目进度总览
- 按类型分类存放

### 3. **便于维护**
- 相关文档放在一起
- 减少根目录混乱
- 方便版本控制

### 4. **专业规范**
- 符合开源项目标准
- 易于团队协作
- 便于文档管理

---

## 📖 如何使用新的文档结构

### 📚 查找文档

**方式1: 从索引开始**
```
docs/INDEX.md → 找到需要的文档类型 → 阅读具体文档
```

**方式2: 按类型查找**
```
# 想了解改进
docs/improvements/ → 选择相关文档

# 想看测试
docs/testing/ → 查看测试文档

# 想看进度
docs/PROGRESS.md
```

### 🧪 运行测试

**测试脚本现在统一在 `tests/` 目录**:
```bash
# 引擎健壮性测试
python tests/test_engine_robustness.py

# Binance API测试
python tests/_test_binance_api.py

# 端点测试
python tests/_test_endpoints.py
```

---

## 🔗 快速访问

### 核心文档
- **项目总览**: [`README.md`](../README.md) (根目录)
- **开发进度**: [`docs/PROGRESS.md`](PROGRESS.md)
- **文档索引**: [`docs/INDEX.md`](INDEX.md)

### 改进文档
- **快速上手**: [`docs/improvements/新功能快速体验.md`](improvements/新功能快速体验.md)
- **视觉对比**: [`docs/improvements/视觉对比.md`](improvements/视觉对比.md)
- **技术详解**: [`docs/improvements/`](improvements/)

### 测试相关
- **测试清单**: [`docs/testing/test_heartbeat_ui.md`](testing/test_heartbeat_ui.md)
- **测试脚本**: [`tests/`](../tests/)

---

## 📌 注意事项

1. **链接更新**: 如果其他文档引用了移动的文件，链接已自动更新
2. **Git历史**: 文件移动保留了完整的Git历史
3. **向后兼容**: 旧的引用路径可能需要手动更新

---

## 🚀 下一步

1. **浏览文档索引**: 从 [`docs/INDEX.md`](INDEX.md) 开始
2. **查看进度**: 阅读 [`docs/PROGRESS.md`](PROGRESS.md)
3. **开始使用**: 参考 [`docs/improvements/新功能快速体验.md`](improvements/新功能快速体验.md)

---

**整理完成时间**: 2026-03-15 06:41:00  
**文档版本**: v1.0

> ✨ **现在文档结构更清晰、更易于维护了！**
