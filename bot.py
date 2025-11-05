# bot.py (main entry point)

import asyncio
import traceback

import config
from utils import logger, display, error_logger, rejection_log_file, VolatilePairScanner, rm_global
from order_manager import OrderManager
from strategy import MomentumSniper
from ws_manager import WSManager

async def main():
    logger.info(f"🚀 Starting Momentum Sniper v8.7.1...")
    display.current_status = "Initializing..."
    if config.DISPLAY_MODE == "TUI": await display.render()

    # Initialize all components
    order_manager = OrderManager()
    scanner = VolatilePairScanner(order_manager)
    sniper = MomentumSniper(scanner, order_manager, rm_global)
    public_ws = WSManager(sniper, is_private=False)
    private_ws = WSManager(sniper, is_private=True)
    sniper.set_ws_managers(public_ws, private_ws)

    try:
        await order_manager.initialize_session()
        display.usdt_balance = order_manager.usdt_balance
        display.current_status = "Performing initial scan..."
        if config.DISPLAY_MODE == "TUI": await display.render()
        
        initial_movers = await scanner.scan_for_volatile_pairs()
        if initial_movers:
            hot_pairs = [m["instId"] for m in initial_movers]
            sniper.hot_pairs = set(hot_pairs)
            display.hot_pairs = sorted(list(sniper.hot_pairs))
            display.sniper_ref = sniper
            public_ws.initial_pairs = hot_pairs
            logger.info(f"✓ Initial scan found {len(hot_pairs)} volatile pairs.")
        else:
            logger.warning("⚠️ Initial scan found no pairs! Check API connectivity.")
        
        tasks = [
            asyncio.create_task(sniper.run_tasks()),
            asyncio.create_task(public_ws.run()),
            asyncio.create_task(private_ws.run()),
            asyncio.create_task(display.render_loop()),
        ]
        display.current_status = "Running..."
        await asyncio.gather(*tasks)

    except (KeyboardInterrupt, SystemExit) as e:
        logger.info(f"Shutdown requested ({type(e).__name__})...")
    except Exception as e:
        error_logger.log_error("MAIN_FATAL", "Fatal error in main loop", traceback.format_exc())
    finally:
        display.current_status = "Shutting down..."
        if config.DISPLAY_MODE == "TUI": await display.render()
        
        if 'public_ws' in locals() and public_ws: await public_ws.stop()
        if 'private_ws' in locals() and private_ws: await private_ws.stop()
        if 'order_manager' in locals() and order_manager: await order_manager.close_session()
        if rejection_log_file: rejection_log_file.close()
            
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        [task.cancel() for task in tasks]
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Bot shut down cleanly.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("...shutdown complete.")