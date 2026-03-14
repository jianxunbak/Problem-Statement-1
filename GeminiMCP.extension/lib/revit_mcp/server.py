# -*- coding: utf-8 -*-
import json
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from fastmcp import FastMCP

mcp = FastMCP("Revit2026_MCP", debug=False)

# UIApplication stored from runner.py
_uiapp = None

def set_revit_app(uiapp):
    global _uiapp
    _uiapp = uiapp

def _get_revit_app():
    return _uiapp

def mm_to_ft(mm):
    return mm / 304.8

def ft_to_mm(ft):
    return ft * 304.8

# --- UI LOGIC HELPERS ---

def get_doc_info_ui():
    uiapp = _get_revit_app()
    if not uiapp or not uiapp.ActiveUIDocument:
        return {"error": "No active document."}
    doc = uiapp.ActiveUIDocument.Document
    return {
        "title": doc.Title,
        "path": doc.PathName or "Unsaved Document"
    }

def create_wall_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document
    
    length_mm = params.get('length_mm', 5000)
    height_mm = params.get('height_mm', 3000)
    start_x = params.get('start_x', 0)
    start_y = params.get('start_y', 0)
    
    p1 = DB.XYZ(mm_to_ft(params.get('start_x', 0)), mm_to_ft(params.get('start_y', 0)), 0)
    
    if 'end_x' in params and 'end_y' in params:
        p2 = DB.XYZ(mm_to_ft(params['end_x']), mm_to_ft(params['end_y']), 0)
    else:
        length_mm = params.get('length_mm', 5000)
        p2 = DB.XYZ(mm_to_ft(params.get('start_x', 0) + length_mm), mm_to_ft(params.get('start_y', 0)), 0)
    
    line = DB.Line.CreateBound(p1, p2)
    
    level = DB.FilteredElementCollector(doc).OfClass(DB.Level).FirstElement()
    
    t = DB.Transaction(doc, "MCP: Create Wall")
    t.Start()
    new_wall = DB.Wall.Create(doc, line, level.Id, False)
    param = new_wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
    if param: param.Set(mm_to_ft(params.get('height_mm', 3000)))
    
    thickness_mm = params.get('thickness_mm')
    if thickness_mm:
        wall_types = DB.FilteredElementCollector(doc).OfClass(DB.WallType)
        for wt in wall_types:
            if str(int(thickness_mm)) in wt.Name:
                new_wall.WallType = wt
                break
                
    t.Commit()
    return {"success": True, "wall_id": str(new_wall.Id.Value)}

def get_element_details_ui(element_id_val):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document
    try:
        eid = DB.ElementId(int(element_id_val))
        el = doc.GetElement(eid)
        if not el: return {"error": "Not found"}
        
        info = {"id": element_id_val, "name": el.Name, "category": el.Category.Name if el.Category else "None", "geometry": []}
        
        # Try to get location curves (for walls/lines) or boundary (for floors)
        loc = el.Location
        if hasattr(loc, "Curve"):
            c = loc.Curve
            if isinstance(c, DB.Line):
                info["geometry"].append({
                    "type": "line",
                    "start": {"x": ft_to_mm(c.GetEndPoint(0).X), "y": ft_to_mm(c.GetEndPoint(0).Y)},
                    "end": {"x": ft_to_mm(c.GetEndPoint(1).X), "y": ft_to_mm(c.GetEndPoint(1).Y)}
                })
        
        # If it's a Floor, get its boundaries
        if isinstance(el, DB.Floor):
            # In Revit 2026, we can get Sketches or just query geometry
            opt = DB.Options()
            geo = el.get_Geometry(opt)
            for g in geo:
                if isinstance(g, DB.Solid):
                    for edge in g.Edges:
                        c = edge.AsCurve()
                        if isinstance(c, DB.Line):
                            info["geometry"].append({
                                "type": "line",
                                "start": {"x": ft_to_mm(c.GetEndPoint(0).X), "y": ft_to_mm(c.GetEndPoint(0).Y)},
                                "end": {"x": ft_to_mm(c.GetEndPoint(1).X), "y": ft_to_mm(c.GetEndPoint(1).Y)}
                            })
        
        return info
    except Exception as e:
        return {"error": str(e)}

def delete_walls_ui():
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document
    walls = DB.FilteredElementCollector(doc).OfClass(DB.Wall).ToElementIds()
    if walls.Count == 0: return {"success": True, "message": "No walls."}
    t = DB.Transaction(doc, "MCP: Delete Walls")
    t.Start()
    doc.Delete(walls)
    t.Commit()
    return {"success": True}

def edit_wall_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document
    wall = doc.GetElement(DB.ElementId(int(params['wall_id'])))
    t = DB.Transaction(doc, "MCP: Edit Wall")
    t.Start()
    if 'height_mm' in params:
        wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM).Set(mm_to_ft(params['height_mm']))
    if 'length_mm' in params:
        lc = wall.Location
        old_line = lc.Curve
        new_end = old_line.GetEndPoint(0) + old_line.Direction.Normalize() * mm_to_ft(params['length_mm'])
        lc.Curve = DB.Line.CreateBound(old_line.GetEndPoint(0), new_end)
    t.Commit()
    return {"success": True}

def move_element_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document
    eid = DB.ElementId(int(params['element_id']))
    dx = params.get('dx_mm', 0)
    dy = params.get('dy_mm', 0)
    dz = params.get('dz_mm', 0)
    direction = params.get('direction', '').lower()
    dist = params.get('distance_mm', 0)
    if direction == 'north': dy += dist
    elif direction == 'south': dy -= dist
    elif direction == 'east': dx += dist
    elif direction == 'west': dx -= dist
    
    t = DB.Transaction(doc, "MCP: Move")
    t.Start()
    vec = DB.XYZ(mm_to_ft(dx), mm_to_ft(dy), mm_to_ft(dz))
    DB.ElementTransformUtils.MoveElement(doc, eid, vec)
    t.Commit()
    return {"success": True}

def create_floor_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    import System.Collections.Generic as Generic # type: ignore
    uiapp = _get_revit_app()
    if not uiapp or not uiapp.ActiveUIDocument:
        return {"error": "No active document."}
    doc = uiapp.ActiveUIDocument.Document
    
    w = mm_to_ft(params.get('width_mm', 5000))
    l = mm_to_ft(params.get('length_mm', 5000))
    x = mm_to_ft(params.get('center_x', 0))
    y = mm_to_ft(params.get('center_y', 0))
    
    try:
        p1 = DB.XYZ(x - w/2, y - l/2, 0)
        p2 = DB.XYZ(x + w/2, y - l/2, 0)
        p3 = DB.XYZ(x + w/2, y + l/2, 0)
        p4 = DB.XYZ(x - w/2, y + l/2, 0)
        profile = DB.CurveLoop()
        profile.Append(DB.Line.CreateBound(p1, p2))
        profile.Append(DB.Line.CreateBound(p2, p3))
        profile.Append(DB.Line.CreateBound(p3, p4))
        profile.Append(DB.Line.CreateBound(p4, p1))
        
        # Explicit .NET List for CurveLoop
        loops = Generic.List[DB.CurveLoop]()
        loops.Add(profile)
        
        level = DB.FilteredElementCollector(doc).OfClass(DB.Level).FirstElement()
        if not level: return {"error": "No levels found."}
        
        floor_type = DB.FilteredElementCollector(doc).OfClass(DB.FloorType).FirstElement()
        if not floor_type: return {"error": "No floor types found."}
        
        t = DB.Transaction(doc, "MCP: Create Floor")
        t.Start()
        new_floor = DB.Floor.Create(doc, loops, floor_type.Id, level.Id)
        t.Commit()
        return {"success": True, "floor_id": str(new_floor.Id.Value)}
    except Exception as e:
        return {"error": str(e)}

# --- MCP TOOLS ---

@mcp.tool()
def get_document_info() -> str:
    from .event_handler import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(get_doc_info_ui))

@mcp.tool()
def create_wall(length_mm: float = 5000, height_mm: float = 3000, start_x: float = 0, start_y: float = 0, end_x: float = None, end_y: float = None, thickness_mm: float = 0) -> str:
    """Create a wall. Use length or explicit end coordinates."""
    from .event_handler import mcp_event_handler
    params = locals()
    return json.dumps(mcp_event_handler.run_on_main_thread(create_wall_ui, params))

@mcp.tool()
def get_element_details(element_id: str) -> str:
    """Get geometric details and location of an element."""
    from .event_handler import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(get_element_details_ui, element_id))

@mcp.tool()
def delete_walls() -> str:
    from .event_handler import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(delete_walls_ui))

@mcp.tool()
def edit_wall(wall_id: str, length_mm: float = None, height_mm: float = None) -> str:
    from .event_handler import mcp_event_handler
    params = locals()
    return json.dumps(mcp_event_handler.run_on_main_thread(edit_wall_ui, params))

@mcp.tool()
def move_element(element_id: str, dx_mm: float = 0, dy_mm: float = 0, dz_mm: float = 0, direction: str = "", distance_mm: float = 0) -> str:
    from .event_handler import mcp_event_handler
    params = locals()
    return json.dumps(mcp_event_handler.run_on_main_thread(move_element_ui, params))

@mcp.tool()
def create_floor(width_mm: float, length_mm: float, center_x: float = 0, center_y: float = 0) -> str:
    from .event_handler import mcp_event_handler
    params = locals()
    return json.dumps(mcp_event_handler.run_on_main_thread(create_floor_ui, params))

@mcp.tool()
def create_wall_type(name: str, thickness_mm: float) -> str:
    from .event_handler import mcp_event_handler
    import Autodesk.Revit.DB as DB # type: ignore
    def action():
        uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
        source = DB.FilteredElementCollector(doc).OfClass(DB.WallType).FirstElement()
        t = DB.Transaction(doc, "MCP: Type")
        t.Start()
        nt = source.Duplicate(name)
        cs = nt.GetCompoundStructure()
        layers = cs.GetLayers()
        for i in range(layers.Count):
            if layers[i].Function == DB.MaterialFunctionAssignment.Structure:
                layers[i].Width = mm_to_ft(thickness_mm)
                break
        cs.SetLayers(layers); nt.SetCompoundStructure(cs)
        t.Commit()
        return {"success": True}
    return json.dumps(mcp_event_handler.run_on_main_thread(action))

@mcp.tool()
def create_floor_type(name: str, thickness_mm: float) -> str:
    from .event_handler import mcp_event_handler
    import Autodesk.Revit.DB as DB # type: ignore
    def action():
        uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
        source = DB.FilteredElementCollector(doc).OfClass(DB.FloorType).FirstElement()
        t = DB.Transaction(doc, "MCP: Type")
        t.Start()
        nt = source.Duplicate(name)
        cs = nt.GetCompoundStructure()
        layers = cs.GetLayers()
        for i in range(layers.Count):
            if layers[i].Function == DB.MaterialFunctionAssignment.Structure:
                layers[i].Width = mm_to_ft(thickness_mm)
                break
        cs.SetLayers(layers); nt.SetCompoundStructure(cs)
        t.Commit()
        return {"success": True}
    return json.dumps(mcp_event_handler.run_on_main_thread(action))

def edit_floor_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document
    floor = doc.GetElement(DB.ElementId(int(params['floor_id'])))
    t = DB.Transaction(doc, "MCP: Edit Floor")
    t.Start()
    if 'type_name' in params:
        floor_types = DB.FilteredElementCollector(doc).OfClass(DB.FloorType)
        for ft in floor_types:
            if params['type_name'].lower() in ft.Name.lower():
                floor.FloorType = ft
                break
    if 'offset_mm' in params:
        param = floor.get_Parameter(DB.BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
        if param: param.Set(mm_to_ft(params['offset_mm']))
    t.Commit()
    return {"success": True}

@mcp.tool()
def edit_floor(floor_id: str, type_name: str = None, offset_mm: float = None) -> str:
    from .event_handler import mcp_event_handler
    params = locals()
    return json.dumps(mcp_event_handler.run_on_main_thread(edit_floor_ui, params))
