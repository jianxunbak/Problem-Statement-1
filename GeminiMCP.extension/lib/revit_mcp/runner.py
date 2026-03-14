# -*- coding: utf-8 -*-
# RUNNER VERSION: v9-SHUTDOWN
from __future__ import print_function
import threading
import traceback
import sys
import os
import time
import socket

# --- LOGGING ---
LOG_FILE = os.path.join(os.path.dirname(__file__), "fastmcp_server.log")

def log(msg):
    try:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_FILE, "a") as f:
            f.write("[{}] {}\n".format(timestamp, msg))
            f.flush()
            os.fsync(f.fileno())
    except:
        pass

class SilentIO(object):
    """Replaces Revit's ScriptIO - routes all output to our log file."""
    def write(self, s):
        if s and s.strip():
            log(s.rstrip())
    def flush(self): pass
    def isatty(self): return False
    @property
    def encoding(self): return "utf-8"

# --- GLOBALS ---
_server_thread = None
_uvicorn_server = None  # Reference to the uvicorn Server object for shutdown

def stop_server():
    """Signal the running uvicorn server to stop. Call before starting a new one."""
    global _uvicorn_server
    if _uvicorn_server is not None:
        try:
            _uvicorn_server.should_exit = True
            log("Shutdown: Signaled uvicorn to stop.")
            time.sleep(1)  # Give it a moment to release the port
        except:
            pass
        _uvicorn_server = None

def is_port_in_use(port=8001):
    """Check if port is in use by attempting a connection (not bind)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        result = s.connect_ex(('127.0.0.1', port))
        return result == 0  # 0 = connected = port in use
    except:
        return False
    finally:
        try:
            s.close()
        except:
            pass


def run_uvicorn_process(mcp_instance):
    """Background thread. SilentIO redirect must happen before any library code."""
    global _uvicorn_server
    sys.stdout = SilentIO()
    sys.stderr = SilentIO()
    log("UvicornThread: IO replaced - RUNNING")

    try:
        log("UvicornThread: Importing uvicorn...")
        import uvicorn
        log("UvicornThread: Importing asyncio...")
        import asyncio
        log("UvicornThread: Building SSE app...")
        app = mcp_instance.sse_app()
        log("UvicornThread: SSE app ready.")

        config = uvicorn.Config(
            app=app,
            host="0.0.0.0",
            port=8001,
            loop="asyncio",
            log_level="info",
            log_config={
                "version": 1,
                "disable_existing_loggers": False,
                "handlers": {"null": {"class": "logging.NullHandler"}},
                "loggers": {
                    "uvicorn":        {"handlers": ["null"], "level": "INFO", "propagate": False},
                    "uvicorn.error":  {"handlers": ["null"], "level": "INFO", "propagate": False},
                    "uvicorn.access": {"handlers": ["null"], "level": "INFO", "propagate": False},
                },
            },
            workers=1,
            timeout_keep_alive=30,
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        _uvicorn_server = server  # Store for shutdown

        log("UvicornThread: Creating asyncio loop...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        log("UvicornThread: server.serve() STARTING on 0.0.0.0:8001")
        loop.run_until_complete(server.serve())
        log("UvicornThread: server.serve() exited.")

    except BaseException as e:
        log("UvicornThread KILLED ({}) : {}".format(type(e).__name__, str(e)))
        log(traceback.format_exc())
    finally:
        _uvicorn_server = None
        log("UvicornThread: Thread ending.")


def start_mcp_server():
    """Called by script.py. Returns True on success, False if already running."""
    global _server_thread
    log("Main: start_mcp_server() entered.")

    # If we have a running server, ensure we still refresh the Revit app reference
    if is_port_in_use(8001):
        log("Main: Port 8001 in use. Re-linking existing server context.")
        try:
            from revit_mcp.server import set_revit_app
            set_revit_app(__revit__)
            log("Main: Existing server context re-linked.")
            return True # Tell script.py to proceed as if started
        except Exception as e:
            log("Main: Re-link failed: " + str(e))
            return False

    try:
        import asyncio
        import uvicorn  # Pre-load into sys.modules
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        from revit_mcp.server import mcp, set_revit_app
        from revit_mcp.event_handler import mcp_event_handler  # noqa

        try:
            set_revit_app(__revit__)
            log("Main: UIApplication stored via set_revit_app.")
        except Exception as e:
            log("Main: set_revit_app failed: " + str(e))

        log("Main: All components pre-loaded.")
    except BaseException as e:
        log("Main Init ERROR: " + str(e))
        log(traceback.format_exc())
        return False

    log("Main: Spawning server thread (daemon=True)...")
    _server_thread = threading.Thread(
        target=run_uvicorn_process,
        args=(mcp,),
        name="GeminiMCP_Uvicorn",
        daemon=True,
    )
    _server_thread.start()
    log("Main: Thread started.")
    return True
