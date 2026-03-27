# -*- coding: utf-8 -*-
import os
import json
import urllib.request
import ssl
import threading
import datetime
import time
try:
    import urllib.error
except ImportError:
    pass

class GeminiClient:
    def __init__(self):
        self._load_config()
        self.lock = threading.Lock()
        # REMOVED: Blocking network check in constructor to avoid "Thinking" hangs on background threads.
        # self._test_connectivity()

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
        env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
        self.api_key = None
        self.model = "gemini-1.5-flash"
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        if k.strip() == "GEMINI_API_KEY": self.api_key = v.strip()
                        if k.strip() == "GEMINI_MODEL": self.model = v.strip()

    def log(self, message):
        """Unified logging to the main server log only."""
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Global absolute path for reliability across all Revit/Python threads
        log_path = r"c:\Users\jianxun\Documents\Revit 2026 MCP\revit-MCP\GeminiMCP.extension\lib\revit_mcp\fastmcp_server.log"
        log_msg = "[{}] [Gemini] {}\n".format(timestamp, message)
        
        try:
            with open(log_path, "a") as f:
                f.write(log_msg)
                f.flush()
        except: pass

    def get_tools(self):
        return [
            {
                "function_declarations": [
                    {
                        "name": "get_document_info",
                        "description": "Get current Revit project details."
                    },
                    {
                        "name": "create_wall",
                        "description": "Create a wall. Specify length or use end_x/end_y for precise placement.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "length_mm": {"type": "number"},
                                "height_mm": {"type": "number"},
                                "start_x": {"type": "number"},
                                "start_y": {"type": "number"},
                                "end_x": {"type": "number"},
                                "end_y": {"type": "number"},
                                "thickness_mm": {"type": "number"},
                                "level_name": {"type": "string", "description": "Optional level name or ID (e.g., 'Level 2')."}
                            }
                        }
                    },
                    {
                        "name": "list_elements",
                        "description": "List all elements in a category (wall, floor, level) to see their IDs and spatial locations (bounding boxes).",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "category": {"type": "string", "description": "Category to list: 'wall', 'floor', or 'level'."}
                            },
                            "required": ["category"]
                        }
                    },
                    {
                        "name": "get_element_details",
                        "description": "Get geometric details (lines/points) of a Revit element by ID.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "element_id": {"type": "string"}
                            },
                            "required": ["element_id"]
                        }
                    },
                    {
                        "name": "orchestrate_build",
                        "description": "MANDATORY: Use this for ALL building-wide requests: creating, editing, resizing, or ADDING/REMOVING/INSERTING storeys in multi-story buildings. Works from a single high-level prompt. Do NOT ask for IDs.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "prompt": {"type": "string", "description": "The full building request, e.g. 'create a 10 storey 30x40m building 3m floor-to-floor' or 'edit the building to 20m x 25m'."}
                            },
                            "required": ["prompt"]
                        }
                    },
                    {
                        "name": "edit_entire_building_dimensions",
                        "description": "STATE-AWARE SYNC: Instantly update the entire building (footprint, height, storeys) OR specific individual floors. Use this for ANY 'edit', 'resize', 'add floor', 'remove floor', or 'remodel' request.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "width_mm": {"type": "number", "description": "New building width in mm. Pass 0 if only editing specific floor overrides."},
                                "depth_mm": {"type": "number", "description": "New building depth in mm. Pass 0 if only editing specific floor overrides."},
                                "height_mm": {"type": "number", "description": "Total building height in mm. Pass 0 if only editing specific floor overrides."},
                                "advanced_instructions": {"type": "string", "description": "Pass any floor-specific overrides or constraints requested by the user here."}
                            },
                            "required": []
                        }
                    },
                    {
                        "name": "delete_walls",
                        "description": "Delete all walls."
                    },
                    {
                        "name": "move_element",
                        "description": "Move any element by offset or cardinal direction (North, South, East, West).",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "element_id": {"type": "string"},
                                "dx_mm": {"type": "number"},
                                "dy_mm": {"type": "number"},
                                "dz_mm": {"type": "number"},
                                "direction": {"type": "string", "description": "North (+Y), South (-Y), East (+X), West (-X)"},
                                "distance_mm": {"type": "number"}
                            },
                            "required": ["element_id"]
                        }
                    },
                    {
                        "name": "create_floor",
                        "description": "Create a rectangular floor.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "width_mm": {"type": "number"},
                                "length_mm": {"type": "number"},
                                "center_x": {"type": "number"},
                                "center_y": {"type": "number"},
                                "level_name": {"type": "string", "description": "Optional level name or ID."}
                            },
                            "required": ["width_mm", "length_mm"]
                        }
                    },
                    {
                        "name": "set_parameter",
                        "description": "Set a parameter on an element (e.g., 'Room Bounding', 'Structural', 'Base Offset'). Values are strings.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "element_id": {"type": "string"},
                                "parameter_name": {"type": "string"},
                                "value": {"type": "string"}
                            },
                            "required": ["element_id", "parameter_name", "value"]
                        }
                    },
                    {
                        "name": "query_types",
                        "description": "List available 'wall' or 'floor' types.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "category": {"type": "string"}
                            },
                            "required": ["category"]
                        }
                    },
                    {
                        "name": "create_polygon_floor",
                        "description": "Create a floor from a list of X/Y points.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "points_mm": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "x": {"type": "number"},
                                            "y": {"type": "number"}
                                        }
                                    }
                                },
                                "level_name": {"type": "string", "description": "Optional level name or ID."}
                            },
                            "required": ["points_mm"]
                        }
                    },
                    {
                        "name": "delete_element",
                        "description": "Delete a specific element by ID.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "element_id": {"type": "string"}
                            },
                            "required": ["element_id"]
                        }
                    },
                    {
                        "name": "create_level",
                        "description": "Create a new level at a specific elevation (mm).",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "elevation_mm": {"type": "number"}
                            },
                            "required": ["elevation_mm"]
                        }
                    },
                    {
                        "name": "create_grid",
                        "description": "Create a linear grid between two points.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "x1": {"type": "number"},
                                "y1": {"type": "number"},
                                "x2": {"type": "number"},
                                "y2": {"type": "number"}
                            },
                            "required": ["x1", "y1", "x2", "y2"]
                        }
                    },
                    {
                        "name": "create_arc_grid",
                        "description": "Create a curved grid using start, end, and middle points.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "start_x": {"type": "number"},
                                "start_y": {"type": "number"},
                                "end_x": {"type": "number"},
                                "end_y": {"type": "number"},
                                "mid_x": {"type": "number"},
                                "mid_y": {"type": "number"}
                            },
                            "required": ["start_x", "start_y", "end_x", "end_y", "mid_x", "mid_y"]
                        }
                    },
                    {
                        "name": "edit_grid",
                        "description": "Rename or change the type of a grid line.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "grid_id": {"type": "string"},
                                "name": {"type": "string"},
                                "type_name": {"type": "string"}
                            },
                            "required": ["grid_id"]
                        }
                    },
                    {
                        "name": "create_wall_type",
                        "description": "Create a new wall type with name and thickness.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "thickness_mm": {"type": "number"}
                            },
                            "required": ["name", "thickness_mm"]
                        }
                    },
                    {
                        "name": "create_floor_type",
                        "description": "Create a new floor type with name and thickness.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "thickness_mm": {"type": "number"}
                            },
                            "required": ["name", "thickness_mm"]
                        }
                    },
                    {
                        "name": "create_column",
                        "description": "Create an architectural column at a level and location.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "type_name": {"type": "string", "description": "Partial or full name of the column type (e.g., '610 x 610mm')."},
                                "level_name": {"type": "string", "description": "Level name (e.g., 'Level 1')."},
                                "x": {"type": "number", "description": "X coordinate in mm."},
                                "y": {"type": "number", "description": "Y coordinate in mm."},
                                "rotation_degrees": {"type": "number", "description": "Rotation around vertical axis."}
                            }
                        }
                    },
                    {
                        "name": "create_door",
                        "description": "Place a door inside a wall host.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "wall_id": {"type": "string", "description": "The ID of the wall that will host the door."},
                                "type_name": {"type": "string", "description": "Partial or full name of the door type."},
                                "offset_mm": {"type": "number", "description": "Distance from the start of the wall line."}
                            },
                            "required": ["wall_id"]
                        }
                    },
                    {
                        "name": "create_window",
                        "description": "Place a window inside a wall host.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "wall_id": {"type": "string", "description": "The ID of the wall that will host the window."},
                                "type_name": {"type": "string", "description": "Partial or full name of the window type."},
                                "offset_mm": {"type": "number", "description": "Distance from the start of the wall line."},
                                "sill_height_mm": {"type": "number", "description": "Elevation above level."}
                            },
                            "required": ["wall_id"]
                        }
                    },
                    {
                        "name": "edit_column",
                        "description": "Edit column location, type, or rotation.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "column_id": {"type": "string"},
                                "type_name": {"type": "string"},
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "rotation_degrees": {"type": "number"}
                            },
                            "required": ["column_id"]
                        }
                    },
                    {
                        "name": "edit_hosted_element",
                        "description": "Edit door or window placement or type/sill-height.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "element_id": {"type": "string"},
                                "type_name": {"type": "string"},
                                "offset_mm": {"type": "number"},
                                "sill_height_mm": {"type": "number"}
                            },
                            "required": ["element_id"]
                        }
                    },
                    {
                        "name": "create_type",
                        "description": "Create a new architectural type (wall, floor, door, window, column) by duplicating an existing one.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "category": {"type": "string", "description": "One of: wall, floor, door, window, column"},
                                "new_name": {"type": "string"},
                                "source_type_name": {"type": "string", "description": "Existing type to copy from."},
                                "parameters": {"type": "object", "description": "Optional dict of parameter values (e.g. {'thickness_mm': 200, 'Width': 900})"}
                            },
                            "required": ["category", "new_name"]
                        }
                    },
                    {
                        "name": "query_levels",
                        "description": "List all floors/levels in the project with their elevations.",
                        "parameters": {
                            "type": "object",
                            "properties": {}
                        }
                    },
                    {
                        "name": "place_family_instance",
                        "description": "Place any non-hosted family (furniture, desk, etc.).",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "type_name": {"type": "string", "description": "Partial or full name of the family type."},
                                "level_name": {"type": "string"},
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "z": {"type": "number"},
                                "rotation": {"type": "number"},
                                "parameters": {"type": "object", "description": "Optional dict of parameters to set on placement."}
                            },
                            "required": ["type_name"]
                        }
                    },
                    {
                        "name": "edit_element",
                        "description": "Generic editor for ANY Revit element. Change type, move, rotate, or set multiple parameters.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "element_id": {"type": "string"},
                                "type_name": {"type": "string", "description": "Switch to this type name."},
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "z": {"type": "number"},
                                "rotation_degrees": {"type": "number"},
                                "parameters": {"type": "object", "description": "Dict of parameter names and values."}
                            },
                            "required": ["element_id"]
                        }
                    },
                    {
                        "name": "edit_type",
                        "description": "Edit ANY family type's parameters project-wide.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "type_name": {"type": "string"},
                                "type_id": {"type": "string"},
                                "parameters": {"type": "object"}
                            }
                        }
                    },
                    {
                        "name": "get_parameters",
                        "description": "Get all parameters (instance and type) for a specific element ID.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "element_id": {"type": "string"}
                            },
                            "required": ["element_id"]
                        }
                    },
                    {
                        "name": "sync_building_manifest",
                        "description": "HIGH PERFORMANCE: Create or update a full multi-story building model (levels, walls, floors) from a JSON manifest. Extremely efficient for large projects.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "manifest_json": {"type": "string", "description": "JSON string defining levels, walls, and floors."}
                            },
                            "required": ["manifest_json"]
                        }
                    },
                ]
            }
        ]

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
            from .dispatcher import orchestrator
            # We need the uiapp - luckily we stored it in the bridge
            from .bridge import _uiapp
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
        response_json = self._make_request(url, data)
        if "error" in response_json:
            self.log("Error in generate_content response: " + str(response_json["error"]))
            return "Error: " + str(response_json["error"])
        
        try:
            result = response_json['candidates'][0]['content']['parts'][0]['text']
            self.log("generate_content successful. Result length: {}".format(len(result)))
            return result
        except (KeyError, IndexError):
            self.log("Error: Failed to extract text from manifest response.")
            return "Error: Failed to extract text from manifest response."

    def _make_request(self, url, data, max_retries=3):
        """Low-level urllib request with simple retry logic"""
        self.log("_make_request() to " + url)
        import ssl
        import urllib.request
        ctx = ssl._create_unverified_context()
        self.log("SSL context created.")
        
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Revit-MCP/1.0'
        }
        
        last_err = None
        for attempt in range(max_retries):
            try:
                self.log("Request Attempt {}/{}...".format(attempt + 1, max_retries))
                req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers)
                self.log("Request object created. Starting urlopen (timeout=120s)...")
                with urllib.request.urlopen(req, context=ctx, timeout=120) as f:
                    self.log("urlopen returned. Reading binary data...")
                    raw_data = f.read()
                    self.log("Data read ({} bytes). Decoding...".format(len(raw_data)))
                    decoded = raw_data.decode('utf-8')
                    self.log("Decoded. Parsing JSON...")
                    return json.loads(decoded)
            except Exception as e:
                err_msg = str(e)
                error_body = ""
                if hasattr(e, 'read'):
                    try: 
                        error_body = e.read().decode('utf-8')
                        err_msg += " (Details: " + error_body + ")"
                    except: pass
                
                # Specific check for SSL EOF error which is common on some Windows environments
                if "UNEXPECTED_EOF_WHILE_READING" in err_msg or "EOF occurred" in err_msg:
                    self.log("SSL EOF Error detected. Retrying with fresh connection... (Attempt {}/{})".format(attempt + 1, max_retries))
                
                # Check for Timeout
                if "timeout" in err_msg.lower():
                    self.log("Gemini API Timeout (120s). This large building requires more processing time. Retrying... (Attempt {}/{})".format(attempt+1, max_retries))

                error_body = ""
                if hasattr(e, 'read'):
                    try: error_body = e.read().decode('utf-8')
                    except: pass
                
                if error_body: err_msg += " | Body: " + error_body
                last_err = err_msg
                
                self.log("Request Error (Attempt {}/{}): {}".format(attempt + 1, max_retries, err_msg))
                if attempt < max_retries - 1:
                    time.sleep(1.5 ** attempt) # Slightly faster retries
                    
        return {"error": last_err}

client = GeminiClient()
