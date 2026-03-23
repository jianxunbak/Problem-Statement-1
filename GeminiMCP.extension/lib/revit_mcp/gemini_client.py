# -*- coding: utf-8 -*-
import os
import json
import urllib.request
import ssl
import threading
try:
    import urllib.error
except ImportError:
    pass

class GeminiClient:
    def __init__(self):
        self._load_config()
        self.lock = threading.Lock()

    def _load_config(self):
        env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
        self.api_key = None
        self.model = "gemini-2.0-flash"
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    if "=" in line:
                        k, v = line.strip().split("=", 1)
                        if k.strip() == "GEMINI_API_KEY": self.api_key = v.strip()
                        if k.strip() == "GEMINI_MODEL": self.model = v.strip()

    def log(self, message):
        log_path = os.path.join(os.path.dirname(__file__), "fastmcp_server.log")
        with self.lock:
            with open(log_path, "a") as f:
                import datetime
                f.write("[{}] [Gemini] {}\n".format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), message))

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
                        "description": "MANDATORY: Use this for ALL building-wide requests: creating, editing, or resizing multi-story buildings. Works from a single high-level prompt. Do NOT ask for IDs.",
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
                        "description": "STATE-AWARE SYNC: Instantly update the entire building to new width, depth, and height without needing any element IDs. Use this when the user says 'edit', 'resize', or 'change shape'.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "width_mm": {"type": "number", "description": "New building width in mm (e.g. 20000 for 20m)"},
                                "depth_mm": {"type": "number", "description": "New building depth in mm (e.g. 25000 for 25m)"},
                                "height_mm": {"type": "number", "description": "Total building height in mm (e.g. 30000 for 10 floors at 3m each)"}
                            },
                            "required": ["width_mm", "depth_mm", "height_mm"]
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
                ]
            }
        ]

    def execute_tool(self, name, args):
        import revit_mcp.server as server
        import traceback
        self.log("Executing Tool: {} with args: {}".format(name, args))
        try:
            func = getattr(server, name)
            return func(**args)
        except Exception as e:
            tb = traceback.format_exc()
            self.log("Tool Execution Error: " + tb)
            return json.dumps({"error": str(e), "traceback": tb})

    def chat(self, prompt, history=None):
        """Tool-enabled chat with multi-step tool execution loop"""
        url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent?key={}".format(self.model, self.api_key)
        
        # Build initial contents
        contents = []
        if history:
            for msg in history:
                role = "user" if msg["is_user"] else "model"
                contents.append({"role": role, "parts": [{"text": msg["text"]}]})
        
        # System instructions sourced from agent_prompts.py
        try:
            from revit_mcp.agent_prompts import SPATIAL_BRAIN_SYSTEM_INSTRUCTION
            instr = SPATIAL_BRAIN_SYSTEM_INSTRUCTION
        except Exception:
            instr = "You are a Revit AI Assistant. Use the provided tools. All lengths in MILLIMETERS."
        instr += (
            " IMPORTANT: All lengths are in MILLIMETERS. "
            "For any building-wide request (create, edit, resize, change storeys), ALWAYS use the 'orchestrate_build' or 'edit_entire_building_dimensions' tools IMMEDIATELY! "
            "NEVER just reply to the user that you 'have updated it'. You MUST physically execute the tool call! "
            "NEVER ask for element IDs for massing changes. NEVER say you cannot edit the building."
        )
        full_prompt = "SYSTEM: {}\n\nUSER: {}".format(instr, prompt)
        contents.append({"role": "user", "parts": [{"text": full_prompt}]})

        # --- RE-ACT LOOP ---
        for _ in range(12): # Max 12 steps per turn
            data = {
                "contents": contents,
                "tools": self.get_tools(),
                "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}}
            }
            
            self.log("Sending request to Gemini API...")
            response_json = self._make_request(url, data)
            self.log("Gemini API responded.")

            if "error" in response_json:
                return "Error: API Failure - " + str(response_json.get("error"))
            
            candidates = response_json.get('candidates', [])
            if not candidates:
                return "Error: No candidates returned from AI."
            
            candidate = candidates[0]
            msg_content = candidate.get('content', {})
            parts = msg_content.get('parts', [])
            
            # Add model's response to history
            contents.append({"role": "model", "parts": parts})
            
            # Check for tool calls
            tool_calls = [p.get('functionCall') for p in parts if 'functionCall' in p]
            if not tool_calls:
                # No tools? Return the final text
                for p in parts:
                    if 'text' in p: return p['text']
                return "Done (No text response)."

            # Execute tools
            tool_results_parts = []
            for tc in tool_calls:
                name = tc['name']
                args = tc.get('args', {})
                self.log("Executing Tool: {} with args: {}".format(name, str(args)))
                
                try:
                    res_raw = self.execute_tool(name, args)
                    res_text = str(res_raw)
                except Exception as e:
                    res_text = json.dumps({"error": str(e)})
                
                tool_results_parts.append({
                    "functionResponse": {
                        "name": name,
                        "response": {"name": name, "content": res_text}
                    }
                })
            
            # Append tool results to conversation as "user" (or rather "function")
            # In Gemini API v1beta, function results are sent as a message with role 'user' (or omitted in some contexts, but 'user' works for part-matching)
            # Actually, Gemini v1beta expects function results in a message following the model's call.
            contents.append({"role": "user", "parts": tool_results_parts})
            
            # Continue the loop for the AI to process results...
            
        return "Error: Multi-step limit reached (12 steps)."

    def generate_content(self, prompt):
        """Pure text generation for internal agent manifest generation"""
        url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent?key={}".format(self.model, self.api_key)
        data = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "thinkingConfig": {"thinkingBudget": 0}
            }
        }
        response_json = self._make_request(url, data)
        if "error" in response_json: return "Error: " + str(response_json["error"])
        
        try:
            return response_json['candidates'][0]['content']['parts'][0]['text']
        except (KeyError, IndexError):
            return "Error: Failed to extract text from manifest response."

    def _make_request(self, url, data, max_retries=3):
        import time
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Revit-MCP/1.0'
        }
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers)
        ctx = ssl._create_unverified_context()
        
        last_err = None
        for attempt in range(max_retries):
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=60) as f:
                    return json.loads(f.read().decode('utf-8'))
            except Exception as e:
                error_body = ""
                if hasattr(e, 'read'):
                    try: error_body = e.read().decode('utf-8')
                    except: pass
                
                err_msg = str(e)
                if error_body: err_msg += " | Body: " + error_body
                last_err = err_msg
                
                self.log("Request Error (Attempt {}/{}): {}".format(attempt + 1, max_retries, err_msg))
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    
        return {"error": last_err}

client = GeminiClient()
