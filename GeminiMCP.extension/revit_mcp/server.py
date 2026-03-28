# -*- coding: utf-8 -*-
import json
import time
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from fastmcp import FastMCP

from revit_mcp.state_manager import state_manager
from revit_mcp.building_generator import BuildingSystem
from revit_mcp import tool_logic as logic

mcp = FastMCP("Revit2026_MCP", debug=False)

# UIApplication stored from runner.py
_uiapp = None

def set_revit_app(uiapp):
    global _uiapp
    _uiapp = uiapp

def _get_revit_app():
    return _uiapp

# Initialize logic with app access
logic.initialize(_get_revit_app)

# --- MCP TOOLS ---

@mcp.tool()
def get_document_info() -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.get_doc_info_ui))

@mcp.tool()
def create_wall(length_mm: float = 5000, height_mm: float = 3000, start_x: float = 0, start_y: float = 0, end_x: float = None, end_y: float = None, thickness_mm: float = 0, level_name: str = "") -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.create_wall_ui, locals()))

@mcp.tool()
def get_element_details(element_id: str) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.get_element_details_ui, element_id))

@mcp.tool()
def delete_walls() -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.delete_walls_ui))

@mcp.tool()
def move_element(element_id: str, dx_mm: float = 0, dy_mm: float = 0, dz_mm: float = 0, direction: str = "", distance_mm: float = 0) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.move_element_ui, locals()))

@mcp.tool()
def create_floor(width_mm: float, length_mm: float, center_x: float = 0, center_y: float = 0, level_name: str = "") -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.create_floor_ui, locals()))

@mcp.tool()
def create_type(category: str, new_name: str, source_type_name: str = "", parameters: dict = None) -> str:
    from revit_mcp.bridge import mcp_event_handler
    params = {"category": category, "new_name": new_name, "source_type_name": source_type_name, "parameters": parameters or {}}
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.duplicate_family_type_ui, params))

@mcp.tool()
def list_family_types(category: str) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.query_types_ui, category))

@mcp.tool()
def list_elements(category: str) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.list_elements_ui, category))

@mcp.tool()
def set_parameter(element_id: str, parameter_name: str, value: str) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.set_parameter_ui, locals()))

@mcp.tool()
def query_levels() -> str:
    from revit_mcp.bridge import mcp_event_handler
    def action():
        import Autodesk.Revit.DB as DB # type: ignore
        doc = _uiapp.ActiveUIDocument.Document
        cl = DB.FilteredElementCollector(doc).OfClass(DB.Level)
        return [{"id": str(lvl.Id.Value), "name": lvl.Name, "elevation": (lvl.Elevation * 304.8)} for lvl in cl]
    return json.dumps(mcp_event_handler.run_on_main_thread(action))

@mcp.tool()
def get_parameters(element_id: str) -> str:
    from revit_mcp.bridge import mcp_event_handler
    def action():
        import Autodesk.Revit.DB as DB # type: ignore
        el = _uiapp.ActiveUIDocument.Document.GetElement(DB.ElementId(int(element_id)))
        res = []
        for p in el.Parameters:
            val = p.AsValueString() or "None"
            res.append({"name": p.Definition.Name, "value": val, "id": str(p.Id.Value)})
        return res
    return json.dumps(mcp_event_handler.run_on_main_thread(action))

@mcp.tool()
def query_types(category: str) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.query_types_ui, category))

@mcp.tool()
def create_polygon_floor(points_mm: list, level_name: str = "") -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.create_polygon_floor_ui, locals()))

@mcp.tool()
def delete_element(element_id: str) -> str:
    from revit_mcp.bridge import mcp_event_handler
    def action():
        import Autodesk.Revit.DB as DB # type: ignore
        doc = _uiapp.ActiveUIDocument.Document
        t = DB.Transaction(doc, "MCP: Delete"); t.Start(); doc.Delete(DB.ElementId(int(element_id))); t.Commit()
        return {"success": True}
    return json.dumps(mcp_event_handler.run_on_main_thread(action))

@mcp.tool()
def create_level(elevation_mm: float) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.create_level_ui, elevation_mm))

@mcp.tool()
def create_grid(x1: float, y1: float, x2: float, y2: float) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.create_grid_ui, locals()))

@mcp.tool()
def create_arc_grid(start_x: float, start_y: float, end_x: float, end_y: float, mid_x: float, mid_y: float) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.create_arc_grid_ui, locals()))

@mcp.tool()
def edit_grid(grid_id: str, name: str = None, type_name: str = None) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.edit_grid_ui, locals()))

@mcp.tool()
def create_column(type_name: str = "", level_name: str = "", x: float = 0, y: float = 0, rotation_degrees: float = 0) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.create_column_ui, locals()))

@mcp.tool()
def create_door(wall_id: str, type_name: str = "", offset_mm: float = 1000) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.create_hosted_ui, {"wall_id": wall_id, "type_name": type_name, "offset_mm": offset_mm, "category": "door"}))

@mcp.tool()
def create_window(wall_id: str, type_name: str = "", offset_mm: float = 1000, sill_height_mm: float = 900) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.create_hosted_ui, {"wall_id": wall_id, "type_name": type_name, "offset_mm": offset_mm, "sill_height_mm": sill_height_mm, "category": "window"}))

@mcp.tool()
def edit_column(column_id: str, type_name: str = "", x: float = -1.0, y: float = -1.0, rotation_degrees: float = -1.0) -> str:
    from revit_mcp.bridge import mcp_event_handler
    params = {"column_id": column_id, "type_name": type_name if type_name else None, "x": x if x != -1.0 else None, "y": y if y != -1.0 else None, "rotation_degrees": rotation_degrees if rotation_degrees != -1.0 else None}
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.edit_column_ui, params))

@mcp.tool()
def edit_hosted_element(element_id: str, type_name: str = "", offset_mm: float = -1.0, sill_height_mm: float = -1.0) -> str:
    from revit_mcp.bridge import mcp_event_handler
    params = {"element_id": element_id, "type_name": type_name if type_name else None, "offset_mm": offset_mm if offset_mm != -1.0 else None, "sill_height_mm": sill_height_mm if sill_height_mm != -1.0 else None}
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.edit_hosted_element_ui, params))

@mcp.tool()
def duplicate_family_type(category: str, new_name: str, source_type_name: str = "", parameters: dict = None) -> str:
    from revit_mcp.bridge import mcp_event_handler
    params = {"category": category, "new_name": new_name, "source_type_name": source_type_name, "parameters": parameters or {}}
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.duplicate_family_type_ui, params))

@mcp.tool()
def place_family_instance(type_name: str, level_name: str = "", x: float = 0, y: float = 0, z: float = 0, rotation: float = 0, parameters: dict = None) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.place_family_instance_ui, locals()))

@mcp.tool()
def edit_element(element_id: str, type_name: str = "", x: float = -1.0, y: float = -1.0, z: float = -1.0, rotation_degrees: float = -1.0, parameters: dict = None) -> str:
    from revit_mcp.bridge import mcp_event_handler
    params = {"element_id": element_id, "type_name": type_name if type_name else None, "x": x if x != -1.0 else None, "y": y if y != -1.0 else None, "z": z if z != -1.0 else None, "rotation_degrees": rotation_degrees if rotation_degrees != -1.0 else None, "parameters": parameters or {}}
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.edit_element_ui, params))

@mcp.tool()
def edit_type(type_name: str = "", type_id: str = "", parameters: dict = None) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(logic.edit_type_ui, {"type_name": type_name, "type_id": type_id, "parameters": parameters or {}}))

@mcp.tool()
def orchestrate_build(prompt: str) -> str:
    from . import dispatcher
    return dispatcher.orchestrator.run_full_stack(_uiapp, prompt)

@mcp.tool()
def edit_entire_building_dimensions(**kwargs) -> str:
    from .gemini_client import client
    from revit_mcp.bridge import mcp_event_handler
    
    lc, eh, consistent = mcp_event_handler.run_on_main_thread(logic.get_building_metrics_ui)
    
    prompt_parts = []
    for k, v in kwargs.items():
        if v and k != 'advanced_instructions':
            prompt_parts.append("{}mm {}".format(v, k.split('_')[0]))
    
    prompt = "Modify building: " + ", ".join(prompt_parts)
    if kwargs.get('advanced_instructions'):
        prompt += ". " + kwargs['advanced_instructions']
    
    return orchestrate_build(prompt)

@mcp.tool()
def generate_building_system(width_mm: float, depth_mm: float, height_mm: float) -> str:
    return edit_entire_building_dimensions(width_mm=width_mm, depth_mm=depth_mm, height_mm=height_mm)

@mcp.tool()
def sync_building_manifest(manifest_json: str) -> str:
    from revit_mcp.bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(lambda: BuildingSystem(_uiapp.ActiveUIDocument.Document).sync_manifest(json.loads(manifest_json))))

@mcp.tool()
def check_bridge_health() -> str:
    from .bridge import _external_event, _handler
    status = {"bridge": "OK" if _external_event else "MISSING", "handler": "OK" if _handler else "MISSING"}
    return json.dumps(status)

@mcp.tool()
def heartbeat() -> str:
    return json.dumps({"status": "ALIVE", "time": time.time()})
