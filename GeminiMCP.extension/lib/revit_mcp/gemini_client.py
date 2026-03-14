# -*- coding: utf-8 -*-
import os
import json
import urllib.request
import ssl
import threading

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
                        if k == "GEMINI_API_KEY": self.api_key = v
                        if k == "GEMINI_MODEL": self.model = v

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
                                "thickness_mm": {"type": "number"}
                            }
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
                                "center_y": {"type": "number"}
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

        instr = "You are a Revit AI Assistant. Use the provided tools (create_wall, delete_walls, edit_wall, move_element, create_floor, create_wall_type, create_floor_type) to help the user. After success, say 'Success: Done! [Summary]'. If failure, start with 'Error: '. Use mm for all dimensions. Directions: North=+Y, South=-Y, East=+X, West=-X."
        
        data = {
            "contents": contents,
            "system_instruction": {"parts": [{"text": instr}]},
            "tools": self.get_tools()
        }

        for _ in range(5):
            response_json = self._make_request(url, data)
            if "error" in response_json: return "Error: " + str(response_json["error"])
            
            candidate = response_json.get('candidates', [{}])[0]
            parts = candidate.get('content', {}).get('parts', [])
            
            tool_calls = [p.get('functionCall') for p in parts if 'functionCall' in p]
            if not tool_calls:
                text_parts = [p.get('text') for p in parts if 'text' in p]
                return "\n".join(text_parts) if text_parts else "No text response."

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

        return "Error: Max iterations reached."

    def _make_request(self, url, data):
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, context=ctx, timeout=30) as f:
            return json.loads(f.read().decode('utf-8'))

client = GeminiClient()
