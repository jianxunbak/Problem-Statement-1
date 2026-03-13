# -*- coding: utf-8 -*-
from __future__ import print_function
import threading
import traceback
import sys
import os
import time
import logging
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

# --- GLOBALS ---
_server_thread = None
_mcp_instance = None

def run_uvicorn_process(mcp_instance):
    """The main server thread."""
    log("UvicornThread: Initializing...")
    try:
        host = "0.0.0.0"
        port = 8001
        
        logging.basicConfig(filename=LOG_FILE, level=logging.DEBUG)
        
        log("UvicornThread: Generating App...")
        app = mcp_instance.sse_app()
        
        import uvicorn
        config = uvicorn.Config(
            app=app, 
            host=host, 
            port=port, 
            loop="asyncio", 
            log_level="debug",
            log_config=None,
            workers=1
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None
        
        log("UvicornThread: Starting Loop on {}:{}".format(host, port))
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())
        
    except Exception as e:
        log("UvicornThread FATAL: " + str(e))
        log(traceback.format_exc())

def start_mcp_server():
    """
    Triggered by the button. 
    Moved loading here so it happens AFTER pyRevit initialization.
    """
    global _mcp_instance
    global _server_thread

    log("Main: start_mcp_server() entered")

    # Check if already running
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 8001))
        s.close()
    except:
        print("Gemini: Server is already active on port 8001.")
        s.close()
        return

    # Load MCP and setup asyncio policy ON THE MAIN THREAD
    try:
        import asyncio
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
        # Import mcp only now. This avoids the early instantiation conflict.
        from revit_mcp.server import mcp
        _mcp_instance = mcp
        log("Main: mcp loaded successfully")
    except Exception as e:
        print("Gemini: Failed to load server instance: {}".format(e))
        log("Main: Load failed: " + str(e))
        log(traceback.format_exc())
        return

    log("Main: Spawning Server Thread")
    _server_thread = threading.Thread(
        target=run_uvicorn_process, 
        args=(_mcp_instance,), 
        name="Gemini_Uvicorn", 
        daemon=True
    )
    _server_thread.start()
    
    print("Gemini: Server spawned in background on port 8001.")
    log("Main: Thread started")
