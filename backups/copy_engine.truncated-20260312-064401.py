    def start(self) -> None:
        with self._state_lock:
            if self._running:
                return
            if self._bn_thread and self._bn_thread.is_alive():
                logger.warning("Binance sync thread is already running; skip duplicate start request")
                return

            # Reset duplicate detection windows before starting a fresh sync loop.
            self._bn_seen = {pid: (int((time.time() - 7200) * 1000), "") for pid in self._bn_seen}
            self._bn_dup_logged.clear()
            self._reconcile_watch.clear()
            self._research_dirty_traders.clear()
            self._running = True
            self._bn_thread = threading.Thread(target=self._run_binance, daemon=True)
            self._bn_thread.start()
            logger.info("Binance sync thread started")
