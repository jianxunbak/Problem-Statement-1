# -*- coding: utf-8 -*-
import threading
import traceback
import sys

_server_thread = None

class FlushFile:
    def __init__(self, f):
        self.f = f
    def write(self, x):
        self.f.write(x)
        self.f.flush()
    def flush(self):
        self.f.flush()
        
def run_server():
    """Runs the FastMCP SSE server."""
    import os
    import traceback
    log_file_path = os.path.join(os.path.dirname(__file__), "fastmcp_server.log")
    
    def log(msg):
        with open(log_file_path, "a") as f:
            f.write(msg + "\n")
            
    log("--- NEW SERVER RUN ---")
    log("Background thread started. Checking imports...")
    
    try:
        import sys
        log("Python Version: " + sys.version)
        import asyncio
        log("Imported asyncio.")
        
        if sys.platform == 'win32':
            # Python 3.8+ defaults to ProactorEventLoop on Windows.
            # ProactorEventLoop is notoriously unstable on background threads 
            # and inside embedded interpreters like Python.Net.
            # We must force the older, thread-safe SelectorEventLoop.
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            log("Enforced WindowsSelectorEventLoopPolicy for thread safety.")
        
        try:
            from revit_mcp.server import mcp
            log("Imported mcp from revit_mcp.server.")
        except Exception as e:
            log("Error importing mcp: " + str(e))
            log(traceback.format_exc())
            return
            
        host = "0.0.0.0"
        port = 8000
        log("Listening on {}:{}".format(host, port))
        
        import uvicorn
        log("Imported uvicorn.")
        
        app = mcp._mcp_server.create_starlette_app()
        log("Created Starlette app.")
        
        config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            loop="asyncio",
            log_level="info"
        )
        
        log("Building Uvicorn server...")
        server = uvicorn.Server(config)
        
        log("Disabling signal handlers...")
        server.install_signal_handlers = lambda: None
        
        log("Uvicorn Server built. Starting event loop serving...")
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        log("Asyncio loop created. Serving...")
        try:
            loop.run_until_complete(server.serve())
        except KeyboardInterrupt:
            log("Server stopped by user.")
        finally:
            loop.close()
            log("Asyncio loop closed.")
            
    except Exception as e:
        log("FATAL THREAD ERROR: " + str(e))
        log(traceback.format_exc())

def start_mcp_server():
    global _server_thread
    
    if _server_thread is not None and _server_thread.is_alive():
        print("MCP Server is already running.")
        return
        
    print("Pre-loading C-extensions on main thread...")
    try:
        import asyncio
        if sys.platform == 'win32':
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        import uvicorn
        from revit_mcp.server import mcp
        print("Main thread pre-load complete.")
    except Exception as e:
        print("MAIN THREAD IMPORT FAILED: " + str(e))
        import traceback
        traceback.print_exc()
        raise e
        
    print("Initializing background thread for FastMCP...")
    # Using daemon=True so the server dies if Revit is closed
    _server_thread = threading.Thread(target=run_server, daemon=True)
    _server_thread.start()
    print("Background thread started. Waiting for SSE client at http://<VM_IP>:8000/sse")
