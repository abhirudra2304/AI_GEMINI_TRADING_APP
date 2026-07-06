# c:\AI_GEMINI_TRADING_APP\lifecycle_manager.py
import threading
import logging
import signal
from typing import Callable, List, Tuple, Dict, Any

logger = logging.getLogger(__name__)

class LifecycleManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(LifecycleManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, '_initialized'):
            self._initialized = True
            self._shutdown_event = threading.Event()
            self._callbacks: List[Tuple[Callable[..., Any], Tuple[Any, ...], Dict[str, Any]]] = []
            self._is_shutting_down = False
            self.register_signal_handlers()

    @property
    def shutdown_event(self) -> threading.Event:
        return self._shutdown_event

    def is_shutdown(self) -> bool:
        """Returns True if the shutdown process has been initiated."""
        return self._shutdown_event.is_set()

    def register(self, callback: Callable[..., Any], *args: Any, **kwargs: Any):
        """
        Register a cleanup function to be called on shutdown.
        Callbacks are executed in LIFO order.
        """
        if self.is_shutdown():
            logger.warning("Shutdown in progress, not registering new callback.")
            return

        logger.debug(f"Registering cleanup callback: {callback.__name__}")
        self._callbacks.append((callback, args, kwargs))

    def _execute_callbacks(self):
        """Execute all registered callbacks in reverse order."""
        print("Executing cleanup callbacks...")
        logger.info("Executing cleanup callbacks...")
        for callback, args, kwargs in reversed(self._callbacks):
            try:
                logger.debug(f"Executing cleanup callback: {callback.__name__}")
                callback(*args, **kwargs)
            except Exception as e:
                logger.error(f"Error during cleanup callback {callback.__name__}: {e}", exc_info=True)
        print("All cleanup callbacks executed.")
        logger.info("All cleanup callbacks executed.")


    def initiate_shutdown(self, signum=None, frame=None):
        """Initiates the graceful shutdown process. Can be called from a signal handler."""
        if self._is_shutting_down:
            return
        
        with self._lock:
            if self._is_shutting_down:
                return
            self._is_shutting_down = True

        if signum:
            signal_name = signal.Signals(signum).name
            print(f"Received signal {signal_name}. Initiating graceful shutdown...")
            logger.info(f"Received signal {signal_name}. Initiating graceful shutdown...")
        else:
            print("Initiating graceful shutdown...")
            logger.info("Initiating graceful shutdown...")

        self._shutdown_event.set()
        self._execute_callbacks()

        print("Shutdown complete. Goodbye.")
        logging.shutdown()

    def register_signal_handlers(self):
        """Registers signal handlers for SIGINT and SIGTERM."""
        signal.signal(signal.SIGINT, self.initiate_shutdown)
        signal.signal(signal.SIGTERM, self.initiate_shutdown)

# Singleton instance
shutdown_manager = LifecycleManager()
