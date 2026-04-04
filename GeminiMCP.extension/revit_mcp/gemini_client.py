import os
import json
import threading
import time
import datetime

try:
    import httpx
except ImportError:
    # Fallback to sys.path if lib not yet recognized
    import sys
    _cur = os.path.dirname(os.path.abspath(__file__))
    lib = os.path.join(os.path.dirname(_cur), "lib")
    if lib not in sys.path: sys.path.append(lib)
    import httpx

from revit_mcp.tool_definitions import TOOL_DECLARATIONS

class GeminiClient:
    def __init__(self):
        self._load_config()
        self.lock = threading.Lock()
        self._init_session()
        self.log("GeminiClient: Persistent httpx session initialized.")

    def _init_session(self):
        """Initializes or resets the persistent httpx session."""
        if hasattr(self, "session") and self.session:
            try: self.session.close()
            except: pass
        self.session = httpx.Client(
            timeout=120.0,
            headers={"User-Agent": "Revit-MCP-Httpx/3.0"},
            follow_redirects=True,
            verify=False 
        )

    def _test_connectivity(self):
        """Minimal ping to verify internet access and API key validity."""
        try:
            self.log("DEBUG: Testing Google API connectivity...")
            url = "https://generativelanguage.googleapis.com/v1beta/models?key={}".format(self.api_key)
            with urllib.request.urlopen(url, timeout=10) as f:
                self.log("DEBUG: Google API Connectivity Check: SUCCESS (Model List Accessible)")
        except Exception as e:
            self.log("DEBUG: Google API Connectivity Check: FAILED - {}".format(str(e)))

    def _load_config(self):
        self.api_key = None
        self.model = "gemini-2.0-flash-exp" # High-performance model
        
        # Consistent path discovery (same level as GeminiMCP.extension/)
        # Using abspath for absolute reliability
        _cur_dir = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.join(os.path.dirname(_cur_dir), ".env")
        
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"): continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() == "GEMINI_API_KEY": self.api_key = v.strip().strip('"').strip("'")
                        if k.strip() == "GEMINI_MODEL": self.model = v.strip().strip('"').strip("'")
        
        # Final safety check
        if not self.api_key:
            from dotenv import load_dotenv # type: ignore
            load_dotenv(env_path)
            self.api_key = os.getenv("GEMINI_API_KEY")
            
        if not self.api_key:
            print("GeminiClient: CRITICAL ERROR - GEMINI_API_KEY not found in " + env_path)

    def log(self, msg):
        import os.path as op
        import datetime
        from revit_mcp.utils import get_log_path
        log_path = get_log_path()
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_msg = "[{}] {}\n".format(timestamp, msg)
        
        try:
            with open(log_path, "a") as f:
                f.write(log_msg)
                f.flush()
        except: pass

    def get_tools(self):
        return TOOL_DECLARATIONS

    def execute_tool(self, name, args):
        import revit_mcp.server as server
        import traceback
        # Redundant log removed (already logged in chat loop)
        try:
            func = getattr(server, name)
            return func(**args)
        except Exception as e:
            tb = traceback.format_exc()
            self.log("Tool Execution Error: " + tb)
            return json.dumps({"error": str(e), "traceback": tb})

    def chat(self, prompt, history=None):
        """High-performance chat routing to Orchestrator."""
        self.log("--- GeminiClient: chat START ---")
        try:
            from revit_mcp.dispatcher import orchestrator
            # We need the uiapp - luckily we stored it in the bridge
            from revit_mcp.bridge import _uiapp
            if not _uiapp:
                from .runner import log as server_log
                server_log("GeminiClient: UIApp missing in bridge, cannot orchestrate.")
                return "Error: Revit connection lost. Please restart the MCP server."
            
            self.log("GeminiClient: Routing to Orchestrator...")
            res = orchestrator.run_full_stack(_uiapp, prompt)
            self.log("GeminiClient: Orchestration complete.")
            return res
        except Exception as e:
            import traceback
            err = "Chat Error: {}\n{}".format(str(e), traceback.format_exc())
            self.log(err)
            return err

    def generate_content(self, prompt):
        """Pure text generation for internal agent manifest generation"""
        self.log("generate_content() entered with model: " + str(self.model))
        url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent?key={}".format(self.model, self.api_key)
        data = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            }
        }
        
        try:
            response_json = self._make_request(url, data)
            if not response_json or "error" in response_json:
                err = response_json.get("error", "Unknown API Error") if response_json else "Empty Response"
                self.log("Error in generate_content: " + str(err))
                return "Error: " + str(err)
            
            result = response_json['candidates'][0]['content']['parts'][0]['text']
            self.log("generate_content successful. Result length: {}".format(len(result)))
            return result
        except Exception as e:
            self.log("generate_content CRASH: " + str(e))
            return "Error: " + str(e)

    def _make_request(self, url, data, max_retries=3):
        """Optimized httpx request with connection pooling and auto-recovery."""
        safe_url = url.split("key=")[0] + "key=********" if "key=" in url else url
        self.log("Requesting (httpx): " + safe_url)
        
        last_err = None
        for attempt in range(max_retries):
            try:
                start_time = time.time()
                resp = self.session.post(url, json=data)
                duration = time.time() - start_time
                
                self.log("Network: Response {} | Time: {:.2f}s | Size: {} bytes".format(
                    resp.status_code, duration, len(resp.content)))
                
                if resp.status_code == 200:
                    return resp.json()
                
                last_err = "HTTP {}".format(resp.status_code)
                self.log("API Error ({}/{}): {}".format(attempt + 1, max_retries, resp.text[:200]))
                if attempt < max_retries - 1: time.sleep(1)
                
            except Exception as e:
                last_err = str(e)
                self.log("Network ERROR (Attempt {}/{}): {}".format(attempt + 1, max_retries, last_err))
                # CRITICAL FIX: On network error, recreate the session to clear the pool
                self._init_session()
                if attempt < max_retries - 1: time.sleep(1)
        
        return {"error": last_err}

client = GeminiClient()
