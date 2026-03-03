#!/usr/bin/env python3
"""
跟单功能测试脚本
测试目标：
1. 价格查询 API 格式修复是否有效
2. 从快照推断持仓是否正常
3. 跟单引擎是否能正常工作
"""
import sys
sys.path.insert(0, '.')

import logging
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s  %(levelname)-8s  %(message)s')
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 测试 1: 价格查询 API
# ─────────────────────────────────────────────────────────────────────────────
def test_ticker_api():
    """测试价格查询 API（修复后格式）"""
    logger.info("=" * 60)
    logger.info("测试 1: 价格查询 API")
    logger.info("=" * 60)

    import copy_engine

    test_cases = [
        ("BTCUSDT_UMCBL", "USDT-FUTURES"),
        ("ETHUSDT_UMCBL", "USDT-FUTURES"),
        ("KITEUSDT_UMCBL", "USDT-FUTURES"),
        ("BCHUSDT_UMCBL", "USDT-FUTURES"),
    ]

    for symbol, product_type in test_cases:
        try:
            price = copy_engine.get_ticker_price(symbol, product_type)
            logger.info(f"  ✅ {symbol}: ${price:,.2f}")
        except Exception as e:
            logger.error(f"  ❌ {symbol}: {e}")

    logger.info("")


# ─────────────────────────────────────────────────────────────────────────────
# 测试 2: 从快照推断持仓
# ─────────────────────────────────────────────────────────────────────────────
def test_infer_positions():
    """测试从本地快照推断持仓"""
    logger.info("=" * 60)
    logger.info("测试 2: 从快照推断持仓")
    logger.info("=" * 60)

    import scraper
    import database as db

    # 获取已启用的交易员
    settings = db.get_copy_settings()
    enabled_traders = settings.get("enabled_traders", "[]")
    import json
    enabled_traders = json.loads(enabled_traders) if enabled_traders else []

    logger.info(f"已启用交易员数量: {len(enabled_traders)}")

    for uid in enabled_traders[:3]:  # 只测试前3个
        logger.info(f"\n  交易员: {uid[:12]}...")

        # 尝试从 currentList 获取
        try:
            curr = scraper.fetch_current_positions(uid)
            logger.info(f"    currentList 返回: {len(curr)} 个持仓")
        except Exception as e:
            logger.warning(f"    currentList 失败: {e}")
            curr = []

        # 如果 currentList 为空，尝试从快照推断
        if not curr:
            logger.info("    currentList 为空，尝试从快照推断...")
            inferred = scraper.infer_current_positions_from_history(uid)
            logger.info(f"    推断持仓: {len(inferred)} 个")
            for pos in inferred[:2]:
                logger.info(f"      - {pos.get('symbol')} {pos.get('direction')} {pos.get('leverage')}x")
        else:
            for pos in curr[:2]:
                logger.info(f"      - {pos.get('symbol')} {pos.get('direction')} {pos.get('leverage')}x")

    logger.info("")


# ─────────────────────────────────────────────────────────────────────────────
# 测试 3: 跟单引擎单轮循环
# ─────────────────────────────────────────────────────────────────────────────
def test_copy_engine():
    """测试跟单引擎是否能正常执行一轮"""
    logger.info("=" * 60)
    logger.info("测试 3: 跟单引擎单轮循环")
    logger.info("=" * 60)

    import copy_engine
    import database as db

    # 获取配置
    settings = db.get_copy_settings()
    if not settings.get("api_key"):
        logger.warning("  未配置 API Key，跳过实际下单测试")
        return

    logger.info(f"  API Key: {settings.get('api_key', '')[:8]}...")
    logger.info(f"  总资金: ${settings.get('total_capital', 0):,.2f}")
    logger.info(f"  最大保证金比例: {settings.get('max_margin_pct', 0)*100:.1f}%")

    # 检查引擎是否已初始化
    if copy_engine._engine is None:
        logger.warning("  跟单引擎未启动，需要从 Web 界面启动")
        return

    # 检查引擎状态
    engine = copy_engine._engine
    logger.info(f"  引擎运行状态: {engine._running}")
    logger.info(f"  失败连续计数: {engine._fail_streak}")

    # 查看当前持仓
    logger.info("\n  当前跟踪的交易员持仓:")
    for uid, positions in engine._prev_snaps.items():
        logger.info(f"    {uid[:12]}...: {len(positions)} 个持仓")

    logger.info("")


# ─────────────────────────────────────────────────────────────────────────────
# 测试 4: 订单执行测试（模拟）
# ─────────────────────────────────────────────────────────────────────────────
def test_order_execution():
    """测试订单执行逻辑"""
    logger.info("=" * 60)
    logger.info("测试 4: 订单执行测试")
    logger.info("=" * 60)

    import order_executor
    import database as db

    settings = db.get_copy_settings()
    if not settings.get("api_key"):
        logger.warning("  未配置 API Key，跳过下单测试")
        return

    api_key = settings["api_key"]
    api_secret = settings["api_secret"]
    api_passphrase = settings["api_passphrase"]

    # 测试 4.1: 获取账户余额
    logger.info("\n  4.1 获取账户余额:")
    try:
        account = order_executor.get_account_balance(api_key, api_secret, api_passphrase)
        logger.info(f"    ✅ 账户模式: {account.get('margin_mode')}")
        available = account.get('available', '0')
        if isinstance(available, str):
            available = float(available)
        logger.info(f"    ✅ 可用余额: ${available:,.2f}")
    except Exception as e:
        logger.error(f"    ❌ {e}")

    # 测试 4.2: 获取当前持仓
    logger.info("\n  4.2 获取当前持仓:")
    try:
        positions = order_executor.get_my_positions(api_key, api_secret, api_passphrase)
        logger.info(f"    ✅ 当前持仓数量: {len(positions)}")
        for pos in positions[:3]:
            logger.info(f"      - {pos.get('symbol')} {pos.get('holdSide')} {pos.get('size')}张")
    except Exception as e:
        logger.error(f"    ❌ {e}")

    # 测试 4.3: 行情查询
    logger.info("\n  4.3 行情查询:")
    test_symbols = ["BTCUSDT", "ETHUSDT", "KITEUSDT"]
    for symbol in test_symbols:
        try:
            price = order_executor.get_ticker_price(symbol)
            logger.info(f"    ✅ {symbol}: ${price:,.2f}")
        except Exception as e:
            logger.error(f"    ❌ {symbol}: {e}")

    logger.info("")


# ─────────────────────────────────────────────────────────────────────────────
# 主函数
# ─────────────────────────────────────────────────────────────────────────────
def main():
    logger.info("\n" + "=" * 60)
    logger.info("开始跟单功能测试")
    logger.info("=" * 60 + "\n")

    # 初始化数据库
    import database as db
    db.init_db()
    logger.info("数据库初始化完成\n")

    # 运行测试
    test_ticker_api()
    test_infer_positions()
    test_copy_engine()
    test_order_execution()

    logger.info("=" * 60)
    logger.info("测试完成")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
