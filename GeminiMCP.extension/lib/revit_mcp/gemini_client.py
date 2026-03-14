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
                        "name": "edit_wall",
                        "description": "Modify an existing wall's parameters.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "wall_id": {"type": "string"},
                                "length_mm": {"type": "number"},
                                "height_mm": {"type": "number"}
                            },
                            "required": ["wall_id"]
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
                        "name": "edit_floor",
                        "description": "Modify an existing floor's type or offset.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "floor_id": {"type": "string"},
                                "type_name": {"type": "string"},
                                "offset_mm": {"type": "number"}
                            },
                            "required": ["floor_id"]
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
                    }
                ]
            }
        ]

    def execute_tool(self, name, args):
        import revit_mcp.server as server
        self.log("Executing Tool: {} with args: {}".format(name, args))
        try:
            func = getattr(server, name)
            return func(**args)
        except Exception as e:
            return json.dumps({"error": str(e)})

    def chat(self, prompt, history=None):
        url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent?key={}".format(self.model, self.api_key)
        contents = []
        if history:
            for msg in history:
                role = "user" if msg["is_user"] else "model"
                contents.append({"role": role, "parts": [{"text": msg["text"]}]})
        contents.append({"role": "user", "parts": [{"text": prompt}]})

        instr = "You are a Revit AI Assistant. Use the provided tools (create_wall, create_floor, create_column, create_door, create_window, create_type, query_levels, edit_column, edit_hosted_element, edit_element, edit_type, place_family_instance, edit_wall, move_element, create_level, create_grid, list_elements, list_family_types, get_element_details, get_parameters, set_parameter) to help the user. After success, say 'Success: Done! [Summary]'. If failure, start with 'Error: '. Use mm for all dimensions. Directions: North=+Y, South=-Y, East=+X, West=-X."
        
        # Move system instruction to user prompt for better compatibility with some keys/projs
        full_prompt = "SYSTEM: {}\n\nUSER: {}".format(instr, prompt)
        
        data = {
            "contents": contents,
            "tools": self.get_tools()
        }
        # Only update the last message if it's the current one
        contents[-1]["parts"][0]["text"] = full_prompt

        for _ in range(15):
            response_json = self._make_request(url, data)
            if "error" in response_json: 
                err_msg = str(response_json["error"])
                self.log("API Error in loop: " + err_msg)
                return "Error: " + err_msg
            
            candidate = response_json.get('candidates', [{}])[0]
            parts = candidate.get('content', {}).get('parts', [])
            
            tool_calls = [p.get('functionCall') for p in parts if 'functionCall' in p]
            if not tool_calls:
                text_parts = [p.get('text') for p in parts if 'text' in p]
                res_text = "\n".join(text_parts) if text_parts else "No text response."
                self.log("AI finished with text: " + res_text[:50] + "...")
                return res_text

            # Process tool calls
            tool_results = []
            for tc in tool_calls:
                res = self.execute_tool(tc['name'], tc.get('args', {}))
                tool_results.append({
                    "functionResponse": {
                        "name": tc['name'],
                        "response": {"name": tc['name'], "content": res}
                    }
                })
            
            contents.append({"role": "model", "parts": parts})
            contents.append({"role": "user", "parts": tool_results})
            data["contents"] = contents

        self.log("ERROR: Max iterations (15) reached.")
        return "Error: Max iterations reached. The task might be too complex or the AI is stuck in a loop."

    def _make_request(self, url, data):
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Revit-MCP/1.0'
        }
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers=headers)
        ctx = ssl._create_unverified_context()
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=30) as f:
                return json.loads(f.read().decode('utf-8'))
        except Exception as e:
            error_body = ""
            if hasattr(e, 'read'):
                try: error_body = e.read().decode('utf-8')
                except: pass
            
            err_msg = str(e)
            if error_body: err_msg += " | Body: " + error_body
            
            self.log("Request Error: {}".format(err_msg))
            return {"error": err_msg}

client = GeminiClient()
