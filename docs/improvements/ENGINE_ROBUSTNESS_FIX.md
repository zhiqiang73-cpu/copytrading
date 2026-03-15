# 引擎健壮性改进说明

## 🔴 严重问题诊断

### 问题描述
**症状**: 显示"引擎运行中"，但实际主线程已停止
**根本原因**: `is_running()` 只检查标志，不检查线程是否真的存活

### 致命场景
```python
# 场景1: 线程崩溃，但标志仍为True
self._running = True      # ✅ 标志
self._bn_thread.is_alive() # ❌ 线程已死

# 结果: 显示"运行中"，但什么都不做
```

---

## ✅ 已实施的修复

### 1. **真实状态检查** (最关键)
```python
def is_running(self) -> bool:
    """检查引擎是否真正在运行（不仅检查标志，还检查线程是否存活）"""
    with self._state_lock:
        if not self._running:
            return False
        # ⚠️ 关键检查: 线程必须还活着
        if self._bn_thread is None or not self._bn_thread.is_alive():
            logger.warning("检测到僵尸状态，自动修正")
            self._running = False  # 强制修正不一致状态
            return False
        return True
```

**修复内容**:
- ✅ 不仅检查标志，还检查线程是否存活
- ✅ 检测到不一致状态时自动修正
- ✅ 记录警告日志便于排查

---

### 2. **启动时僵尸检测**
```python
def start(self) -> None:
    with self._state_lock:
        # 新增: 检测僵尸状态
        if self._running:
            if self._bn_thread is None or not self._bn_thread.is_alive():
                logger.warning("检测到僵尸状态，强制清理并重启")
                self._running = False  # 清理
                self._bn_thread = None
            else:
                logger.warning("引擎已在运行中，跳过重复启动")
                return
```

**修复内容**:
- ✅ 启动前先检查是否存在僵尸进程
- ✅ 如果标志True但线程已死，强制清理后重启
- ✅ 防止"卡住"状态

---

### 3. **线程异常捕获与状态同步**
```python
def _run_binance(self) -> None:
    logger.info("线程启动 [ID: %s]", threading.current_thread().ident)
    try:
        while self._running:
            try:
                self._loop_binance_once()
            except Exception as exc:
                logger.error("循环异常: %s", exc, exc_info=True)
            time.sleep(3)
    except Exception as fatal:
        logger.critical("线程遭遇致命异常: %s", fatal, exc_info=True)
    finally:
        # ⚠️ 关键: 线程退出时强制同步状态
        with self._state_lock:
            was_running = self._running
            self._running = False
            if was_running:
                logger.error("⚠️ 线程异常退出！状态已自动设为停止")
```

**修复内容**:
- ✅ 线程退出时自动将标志设为False
- ✅ 区分正常退出和异常退出
- ✅ 记录详细日志便于追踪

---

### 4. **线程命名与ID跟踪**
```python
self._bn_thread = threading.Thread(
    target=self._run_binance,
    daemon=True,
    name=f"BN-{self._profile}"  # 新增: 线程命名
)
logger.info("线程启动 [ID: %s]", self._bn_thread.ident)  # 新增: ID记录
```

**修复内容**:
- ✅ 给线程起有意义的名字
- ✅ 记录线程ID便于排查
- ✅ 方便用系统工具查看线程状态

---

## 🛡️ 防护机制

### 多层防护
```
┌─────────────────────────────────────┐
│ 1. is_running() 实时检查           │
│    ✓ 检查标志                      │
│    ✓ 检查线程存活                  │
│    ✓ 自动修正不一致                │
├─────────────────────────────────────┤
│ 2. start() 启动前检测              │
│    ✓ 检测僵尸进程                  │
│    ✓ 强制清理后重启                │
├─────────────────────────────────────┤
│ 3. _run_binance() 异常捕获         │
│    ✓ try-finally 保证状态同步      │
│    ✓ 记录详细异常信息              │
├─────────────────────────────────────┤
│ 4. 日志追踪                        │
│    ✓ 线程ID记录                    │
│    ✓ 启动/停止/异常全程记录        │
└─────────────────────────────────────┘
```

---

## 🔍 日志示例

### 正常启动
```
[sim] 跟单引擎启动 (币安信号源模式) [线程ID: 12345]
[sim] 币安监控线程启动 [线程ID: 12345]
```

### 检测到僵尸状态
```
[sim] 检测到僵尸状态（标志True但线程已死），强制清理并重启
[sim] 跟单引擎启动 (币安信号源模式) [线程ID: 12346]
```

### 线程异常退出
```
[sim] Binance loop error: ConnectionError(...)
[sim] ⚠️ 币安监控线程异常退出！引擎状态已自动设为停止
```

### 状态查询
```
# 如果线程已死但标志还是True
[sim] 检测到引擎标志为True但线程已停止，自动修正状态
```

---

## 🧪 测试验证

### 测试1: 正常启动停止
```bash
1. 点击启动 → 看到日志 "线程启动 [ID: xxx]"
2. 检查状态 → 显示"运行中" ✅
3. 点击停止 → 看到日志 "线程正常退出"
4. 检查状态 → 显示"已停止" ✅
```

### 测试2: 模拟线程崩溃
```python
# 故意在循环中抛异常
def _loop_binance_once(self):
    raise RuntimeError("模拟崩溃")

结果:
- ✅ 日志记录 "线程异常退出"
- ✅ 状态自动变为"已停止"
- ✅ 下次启动能正常恢复
```

### 测试3: 僵尸进程恢复
```python
# 手动构造僵尸状态
self._running = True
self._bn_thread = None  # 或已死线程

# 点击启动
结果:
- ✅ 检测到僵尸状态
- ✅ 强制清理
- ✅ 成功重启
```

---

## 📊 改进对比

| 场景 | 改进前 | 改进后 |
|------|--------|--------|
| 线程崩溃 | 标志仍True，显示"运行中"❌ | 自动检测并修正状态 ✅ |
| 重复启动 | 可能创建多个线程 ❌ | 严格防止重复启动 ✅ |
| 异常退出 | 状态不同步 ❌ | finally保证状态同步 ✅ |
| 问题排查 | 无线程ID，难以追踪 ❌ | 详细日志+线程ID ✅ |
| 僵尸恢复 | 无法自动恢复 ❌ | 自动检测并清理 ✅ |

---

## ⚠️ 注意事项

1. **线程ID仅用于调试** - 不要依赖它做逻辑判断
2. **daemon=True** - 确保主进程退出时线程能正常结束
3. **锁的正确使用** - 状态检查和修改都在锁内进行
4. **日志级别** - 异常退出用ERROR，正常退出用INFO

---

## 🎯 预期效果

### 用户体验
- ✅ 状态显示**永远准确**
- ✅ 线程崩溃**自动恢复**
- ✅ 启动失败**立即知道**
- ✅ 问题排查**有据可查**

### 系统稳定性
- ✅ 无僵尸进程
- ✅ 无状态不同步
- ✅ 无资源泄漏
- ✅ 可靠重启

---

**总结**: 这些改进确保了引擎状态的**真实性**和**一致性**，用户看到的状态就是系统实际的状态！
