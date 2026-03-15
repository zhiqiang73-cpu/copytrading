#!/usr/bin/env python3
"""
引擎状态验证脚本
用于测试引擎状态是否真实可靠
"""
import sys
import time
import threading

# 添加项目路径
sys.path.insert(0, '.')

import copy_engine
import database as db

def test_normal_start_stop():
    """测试1: 正常启动停止"""
    print("\n=== 测试1: 正常启动停止 ===")
    
    # 启动
    print("1. 启动引擎...")
    copy_engine.start_engine('sim')
    time.sleep(2)
    
    # 检查状态
    is_running = copy_engine.is_engine_running('sim')
    print(f"2. 检查状态: {'✅ 运行中' if is_running else '❌ 未运行'}")
    
    # 停止
    print("3. 停止引擎...")
    copy_engine.stop_engine('sim')
    time.sleep(2)
    
    # 再次检查
    is_running = copy_engine.is_engine_running('sim')
    print(f"4. 检查状态: {'❌ 还在运行' if is_running else '✅ 已停止'}")
    
    return not is_running

def test_thread_alive_check():
    """测试2: 线程存活检查"""
    print("\n=== 测试2: 线程存活检查 ===")
    
    # 启动引擎
    print("1. 启动引擎...")
    copy_engine.start_engine('sim')
    time.sleep(2)
    
    # 获取引擎实例
    engine = copy_engine._ENGINES.get('sim')
    if not engine:
        print("❌ 无法获取引擎实例")
        return False
    
    # 检查线程
    thread = engine._bn_thread
    print(f"2. 线程对象: {thread}")
    print(f"3. 线程存活: {'✅ 是' if thread and thread.is_alive() else '❌ 否'}")
    print(f"4. 线程ID: {thread.ident if thread else 'N/A'}")
    print(f"5. 线程名称: {thread.name if thread else 'N/A'}")
    
    # 检查状态标志
    print(f"6. 状态标志: {engine._running}")
    print(f"7. is_running(): {'✅ True' if engine.is_running() else '❌ False'}")
    
    # 停止
    copy_engine.stop_engine('sim')
    time.sleep(1)
    
    return thread and thread.is_alive()

def test_zombie_detection():
    """测试3: 僵尸进程检测"""
    print("\n=== 测试3: 僵尸进程检测 ===")
    
    # 启动引擎
    print("1. 启动引擎...")
    copy_engine.start_engine('sim')
    time.sleep(2)
    
    # 获取引擎
    engine = copy_engine._ENGINES.get('sim')
    if not engine:
        print("❌ 无法获取引擎实例")
        return False
    
    # 模拟僵尸状态：停止线程但不修改标志
    print("2. 模拟僵尸状态（停止线程但保持标志）...")
    with engine._state_lock:
        engine._running = False  # 先停止线程
    time.sleep(3)  # 等待线程退出
    
    with engine._state_lock:
        engine._running = True  # 然后把标志改回True (制造僵尸状态)
    
    print(f"3. 当前状态标志: {engine._running}")
    print(f"4. 线程存活: {engine._bn_thread and engine._bn_thread.is_alive()}")
    
    # 调用is_running检查（应该自动修正）
    print("5. 调用 is_running() 进行检测...")
    is_running = engine.is_running()
    print(f"6. is_running() 返回: {'❌ True (未修正)' if is_running else '✅ False (已修正)'}")
    print(f"7. 状态标志已修正为: {engine._running}")
    
    # 尝试重启
    print("8. 尝试重新启动...")
    copy_engine.start_engine('sim')
    time.sleep(2)
    
    is_running_now = copy_engine.is_engine_running('sim')
    print(f"9. 重启后状态: {'✅ 运行中' if is_running_now else '❌ 未运行'}")
    
    # 清理
    copy_engine.stop_engine('sim')
    
    return not is_running and is_running_now

def test_multiple_status_checks():
    """测试4: 多次状态检查"""
    print("\n=== 测试4: 多次状态检查 ===")
    
    print("1. 启动引擎...")
    copy_engine.start_engine('sim')
    time.sleep(2)
    
    print("2. 快速连续检查状态10次...")
    results = []
    for i in range(10):
        is_running = copy_engine.is_engine_running('sim')
        results.append(is_running)
        print(f"   检查 {i+1}/10: {'✅' if is_running else '❌'}")
        time.sleep(0.1)
    
    all_true = all(results)
    print(f"3. 结果: {'✅ 全部一致(True)' if all_true else '❌ 结果不一致'}")
    
    copy_engine.stop_engine('sim')
    
    return all_true

def main():
    """运行所有测试"""
    print("=" * 60)
    print("引擎状态验证测试")
    print("=" * 60)
    
    # 确保数据库已初始化
    try:
        db.init_db()
    except Exception as e:
        print(f"❌ 数据库初始化失败: {e}")
        return
    
    results = []
    
    # 运行测试
    try:
        results.append(("正常启动停止", test_normal_start_stop()))
        results.append(("线程存活检查", test_thread_alive_check()))
        results.append(("僵尸进程检测", test_zombie_detection()))
        results.append(("多次状态检查", test_multiple_status_checks()))
    except Exception as e:
        print(f"\n❌ 测试过程出错: {e}")
        import traceback
        traceback.print_exc()
    
    # 总结
    print("\n" + "=" * 60)
    print("测试总结")
    print("=" * 60)
    for name, result in results:
        status = "✅ 通过" if result else "❌ 失败"
        print(f"{name}: {status}")
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    print(f"\n通过率: {passed}/{total} ({passed/total*100:.1f}%)")
    
    if passed == total:
        print("\n🎉 所有测试通过！引擎状态检查可靠！")
    else:
        print("\n⚠️ 部分测试失败，需要进一步检查")

if __name__ == "__main__":
    main()
