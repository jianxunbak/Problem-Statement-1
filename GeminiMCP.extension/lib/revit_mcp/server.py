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

def mm_to_ft(mm): return mm / 304.8
def ft_to_mm(ft): return ft * 304.8

def get_bip(name):
    import Autodesk.Revit.DB as DB # type: ignore
    try: return getattr(DB.BuiltInParameter, name)
    except: return None

def _find_level(doc, level_id_or_name=None):
    import Autodesk.Revit.DB as DB # type: ignore
    cl = DB.FilteredElementCollector(doc).OfClass(DB.Level)
    if not level_id_or_name:
        return cl.FirstElement()
    
    # Try ID first
    try:
        eid = DB.ElementId(int(level_id_or_name))
        lvl = doc.GetElement(eid)
        if isinstance(lvl, DB.Level): return lvl
    except: pass
    
    # Try Name
    for lvl in cl:
        if level_id_or_name.lower() in lvl.Name.lower():
            return lvl
    return cl.FirstElement()

def _find_type_symbol(doc, category_bip, type_name=None):
    import Autodesk.Revit.DB as DB # type: ignore
    cl = DB.FilteredElementCollector(doc).OfCategory(category_bip).OfClass(DB.ElementType)
    
    if not type_name:
        return cl.FirstElement()
    
    # Try exact match
    for sym in cl:
        if sym.Name.lower() == type_name.lower():
            return sym
    
    # Try partial match
    for sym in cl:
        if type_name.lower() in sym.Name.lower():
            return sym
            
    return cl.FirstElement()

def _set_params_batch(element, params_dict):
    import Autodesk.Revit.DB as DB # type: ignore
    for p_name, p_val in params_dict.items():
        param = element.LookupParameter(p_name)
        if not param:
            bip = get_bip(p_name)
            if bip: param = element.get_Parameter(bip)
        
        if param and not param.IsReadOnly:
            if param.StorageType == DB.StorageType.Double:
                # If it looks like a dimension, convert
                low = p_name.lower()
                if any(x in low for x in ["width", "height", "depth", "thickness", "length", "radius", "diameter", "offset", "sill", "elevation"]):
                    param.Set(mm_to_ft(float(p_val)))
                else:
                    param.Set(float(p_val))
            elif param.StorageType == DB.StorageType.Integer: param.Set(int(p_val))
            elif param.StorageType == DB.StorageType.String: param.Set(str(p_val))
            elif param.StorageType == DB.StorageType.ElementId: param.Set(DB.ElementId(int(p_val)))

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
    
    level = _find_level(doc, params.get('level_name') or params.get('level_id'))
    
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
        
        # Grid handling
        if isinstance(el, DB.Grid):
            c = el.Curve
            info["geometry"].append({
                "type": "grid_line",
                "start": {"x": ft_to_mm(c.GetEndPoint(0).X), "y": ft_to_mm(c.GetEndPoint(0).Y)},
                "end": {"x": ft_to_mm(c.GetEndPoint(1).X), "y": ft_to_mm(c.GetEndPoint(1).Y)}
            })
            
        # Level handling
        elif isinstance(el, DB.Level):
            info["elevation_mm"] = ft_to_mm(el.Elevation)

        # General location curve (Walls, etc.)
        loc = el.Location
        if loc and hasattr(loc, "Curve"):
            c = loc.Curve
            if isinstance(c, DB.Line):
                info["geometry"].append({
                    "type": "line",
                    "start": {"x": ft_to_mm(c.GetEndPoint(0).X), "y": ft_to_mm(c.GetEndPoint(0).Y)},
                    "end": {"x": ft_to_mm(c.GetEndPoint(1).X), "y": ft_to_mm(c.GetEndPoint(1).Y)}
                })
        
        # Point location (Doors, Windows, Columns, Furniture)
        elif loc and hasattr(loc, "Point"):
            p = loc.Point
            info["point"] = {"x": ft_to_mm(p.X), "y": ft_to_mm(p.Y), "z": ft_to_mm(p.Z)}

        # If it's a Floor, get its boundaries
        if isinstance(el, DB.Floor):
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
    
    if params.get('type_name'):
        wt = _find_type_symbol(doc, DB.BuiltInCategory.OST_Walls, params['type_name'])
        if wt: wall.WallType = wt
        
    if params.get('height_mm') is not None:
        p = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
        if p: p.Set(mm_to_ft(float(params['height_mm'])))
        
    if params.get('length_mm') is not None:
        lc = wall.Location
        if hasattr(lc, "Curve") and isinstance(lc.Curve, DB.Line):
            old_line = lc.Curve
            new_end = old_line.GetEndPoint(0) + old_line.Direction.Normalize() * mm_to_ft(float(params['length_mm']))
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
        
        loops = Generic.List[DB.CurveLoop]()
        loops.Add(profile)
        
        level = _find_level(doc, params.get('level_name') or params.get('level_id'))
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

def create_column_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    
    level = _find_level(doc, params.get('level_name'))
    symbol = _find_type_symbol(doc, DB.BuiltInCategory.OST_Columns, params.get('type_name'))
    
    if not symbol.IsActive:
        t = DB.Transaction(doc, "Load Symbol")
        t.Start(); symbol.Activate(); t.Commit()
        
    p = DB.XYZ(mm_to_ft(params.get('x', 0)), mm_to_ft(params.get('y', 0)), level.Elevation)
    
    t = DB.Transaction(doc, "MCP: Column")
    t.Start()
    inst = doc.Create.NewFamilyInstance(p, symbol, level, DB.Structure.StructuralType.NonStructural)
    
    rotation = params.get('rotation_degrees', 0)
    if rotation != 0:
        axis = DB.Line.CreateBound(p, p + DB.XYZ.BasisZ)
        DB.ElementTransformUtils.RotateElement(doc, inst.Id, axis, rotation * (3.14159 / 180.0))
        
    t.Commit()
def create_hosted_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    
    category = DB.BuiltInCategory.OST_Doors if params['category'] == 'door' else DB.BuiltInCategory.OST_Windows
    symbol = _find_type_symbol(doc, category, params.get('type_name'))
    wall = doc.GetElement(DB.ElementId(int(params['wall_id'])))
    
    if not symbol.IsActive:
        t = DB.Transaction(doc, "Activate")
        t.Start(); symbol.Activate(); t.Commit()
        
    lc = wall.Location
    line = lc.Curve
    start = line.GetEndPoint(0)
    direction = line.Direction.Normalize()
    
    offset = mm_to_ft(params.get('offset_mm', 1000))
    p = start + direction * offset
    
    level = doc.GetElement(wall.LevelId)
    
    t = DB.Transaction(doc, "MCP: Hosted")
    t.Start()
    inst = doc.Create.NewFamilyInstance(p, symbol, wall, level, DB.Structure.StructuralType.NonStructural)
    
    sill = params.get('sill_height_mm')
    if sill is not None:
        p_sill = inst.get_Parameter(DB.BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
        if p_sill: p_sill.Set(mm_to_ft(sill))
        
    t.Commit()
    return {"success": True, "id": str(inst.Id.Value)}

def edit_column_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    inst = doc.GetElement(DB.ElementId(int(params['column_id'])))
    
    t = DB.Transaction(doc, "MCP: Edit Column")
    t.Start()
    
    if params.get('type_name'):
        symbol = _find_type_symbol(doc, DB.BuiltInCategory.OST_Columns, params['type_name'])
        if symbol: inst.Symbol = symbol
        
    if params.get('x') is not None or params.get('y') is not None:
        loc = inst.Location
        old_p = loc.Point
        new_x = mm_to_ft(params['x']) if params.get('x') is not None else old_p.X
        new_y = mm_to_ft(params['y']) if params.get('y') is not None else old_p.Y
        new_p = DB.XYZ(new_x, new_y, old_p.Z)
        loc.Point = new_p
        
    if params.get('rotation_degrees') is not None:
        p = inst.Location.Point
        axis = DB.Line.CreateBound(p, p + DB.XYZ.BasisZ)
        DB.ElementTransformUtils.RotateElement(doc, inst.Id, axis, params['rotation_degrees'] * (3.14159 / 180.0))
        
    t.Commit()
    return {"success": True}

def edit_hosted_element_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    inst = doc.GetElement(DB.ElementId(int(params['element_id'])))
    
    t = DB.Transaction(doc, "MCP: Edit Hosted")
    t.Start()
    
    if params.get('type_name'):
        symbol = _find_type_symbol(doc, inst.Category.BuiltInCategory, params['type_name'])
        if symbol: inst.Symbol = symbol
        
    if params.get('offset_mm') is not None:
        wall = inst.Host
        if wall:
            lc = wall.Location
            line = lc.Curve
            p_new = line.GetEndPoint(0) + line.Direction.Normalize() * mm_to_ft(params['offset_mm'])
            loc = inst.Location
            old_p = loc.Point
            vec = p_new - old_p
            DB.ElementTransformUtils.MoveElement(doc, inst.Id, vec)
            
    if params.get('sill_height_mm') is not None:
        p_sill = inst.get_Parameter(DB.BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
        if p_sill: p_sill.Set(mm_to_ft(params['sill_height_mm']))
        
    t.Commit()
    return {"success": True}

def duplicate_family_type_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    
    cat_map = {
        "door": DB.BuiltInCategory.OST_Doors,
        "window": DB.BuiltInCategory.OST_Windows,
        "column": DB.BuiltInCategory.OST_Columns,
        "wall": DB.BuiltInCategory.OST_Walls,
        "floor": DB.BuiltInCategory.OST_Floors
    }
    bip = cat_map.get(params['category'].lower(), DB.BuiltInCategory.OST_Doors)
    source = _find_type_symbol(doc, bip, params.get('source_type_name'))
    if not source: return {"error": "Source type not found"}
    
    t = DB.Transaction(doc, "MCP: Create Type")
    t.Start()
    new_type = source.Duplicate(params['new_name'])
    
    params_dict = params.get('parameters', {})
    
    # Handle System Families (Wall/Floor thickness)
    if params['category'].lower() in ["wall", "floor"] and "thickness_mm" in params_dict:
        try:
            cs = new_type.GetCompoundStructure()
            if cs:
                layers = cs.GetLayers()
                for i in range(layers.Count):
                    if layers[i].Function == DB.MaterialFunctionAssignment.Structure:
                        layers[i].Width = mm_to_ft(float(params_dict["thickness_mm"]))
                        break
                cs.SetLayers(layers)
                new_type.SetCompoundStructure(cs)
        except: pass
        
    _set_params_batch(new_type, params_dict)
    
    t.Commit()
    return {"success": True, "id": str(new_type.Id.Value)}

def place_family_instance_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    
    # Try to find symbol by name across categories if not specified
    cls = DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol)
    symbol = None
    for s in cls:
        if s.Name.lower() == params['type_name'].lower():
            symbol = s; break
    if not symbol:
        for s in cls:
            if params['type_name'].lower() in s.Name.lower():
                symbol = s; break
                
    if not symbol: return {"error": "Symbol not found: " + params['type_name']}
    
    level = _find_level(doc, params.get('level_name'))
    p = DB.XYZ(mm_to_ft(params.get('x', 0)), mm_to_ft(params.get('y', 0)), mm_to_ft(params.get('z', 0)) + level.Elevation)
    
    t = DB.Transaction(doc, "MCP: Place Family")
    t.Start()
    if not symbol.IsActive: symbol.Activate()
    inst = doc.Create.NewFamilyInstance(p, symbol, level, DB.Structure.StructuralType.NonStructural)
    
    rotation = params.get('rotation', 0)
    if rotation != 0:
        axis = DB.Line.CreateBound(p, p + DB.XYZ.BasisZ)
        DB.ElementTransformUtils.RotateElement(doc, inst.Id, axis, rotation * (3.14159 / 180.0))
        
    _set_params_batch(inst, params.get('parameters', {}))
    t.Commit()
    return {"success": True, "id": str(inst.Id.Value)}

def edit_element_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    eid = DB.ElementId(int(params['element_id']))
    el = doc.GetElement(eid)
    
    t = DB.Transaction(doc, "MCP: Edit Element")
    t.Start()
    
    # Type Switch
    if params.get('type_name'):
        new_sym = _find_type_symbol(doc, el.Category.BuiltInCategory, params['type_name'])
        if new_sym:
            if hasattr(el, 'Symbol'): el.Symbol = new_sym
            elif isinstance(el, DB.Wall): el.WallType = new_sym
            elif isinstance(el, DB.Floor): el.FloorType = new_sym
            elif hasattr(el, 'WallType'): el.WallType = new_sym
            elif hasattr(el, 'FloorType'): el.FloorType = new_sym
            
    # Move/Rotate
    move_params = ["x", "y", "z"]
    if any(params.get(p) is not None for p in move_params):
        loc = el.Location
        if hasattr(loc, 'Point'):
            old_p = loc.Point
            new_x = mm_to_ft(params['x']) if params.get('x') is not None else old_p.X
            new_y = mm_to_ft(params['y']) if params.get('y') is not None else old_p.Y
            new_z = mm_to_ft(params['z']) if params.get('z') is not None else old_p.Z
            loc.Point = DB.XYZ(new_x, new_y, new_z)
            
    if params.get('rotation_degrees') is not None:
        p = el.Location.Point if hasattr(el.Location, 'Point') else DB.XYZ.Zero
        axis = DB.Line.CreateBound(p, p + DB.XYZ.BasisZ)
        DB.ElementTransformUtils.RotateElement(doc, el.Id, axis, params['rotation_degrees'] * (3.14159 / 180.0))
        
    # Parameters
    _set_params_batch(el, params.get('parameters', {}))
    
    t.Commit()
    return {"success": True}

def edit_type_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    
    # Find type by ID or generic query
    if params.get('type_id'):
        etype = doc.GetElement(DB.ElementId(int(params['type_id'])))
    else:
        cl = DB.FilteredElementCollector(doc).OfClass(DB.ElementType)
        etype = None
        for t in cl:
            if params['type_name'].lower() in t.Name.lower():
                etype = t; break
                
    if not etype: return {"error": "Type not found"}
    
    t = DB.Transaction(doc, "MCP: Edit Type")
    t.Start()
    _set_params_batch(etype, params.get('parameters', {}))
    t.Commit()
    return {"success": True}

# --- MCP TOOLS ---

@mcp.tool()
def get_document_info() -> str:
    from .bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(get_doc_info_ui))

@mcp.tool()
def create_wall(length_mm: float = 5000, height_mm: float = 3000, start_x: float = 0, start_y: float = 0, end_x: float = 0, end_y: float = 0, thickness_mm: float = 0, level_name: str = "") -> str:
    """Create a wall. Specify length or use end_x/end_y. level_name is optional."""
    from .bridge import mcp_event_handler
    params = locals()
    return json.dumps(mcp_event_handler.run_on_main_thread(create_wall_ui, params))

@mcp.tool()
def get_element_details(element_id: str) -> str:
    """Get geometric details and location of an element."""
    from .bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(get_element_details_ui, element_id))

@mcp.tool()
def delete_walls() -> str:
    from .bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(delete_walls_ui))

@mcp.tool()
def edit_wall(wall_id: str, length_mm: float = -1.0, height_mm: float = -1.0, type_name: str = "") -> str:
    """Edit wall properties. Use -1 for numbers or empty string for type to keep current."""
    from .bridge import mcp_event_handler
    params = {"wall_id": wall_id, 
              "length_mm": length_mm if length_mm != -1.0 else None, 
              "height_mm": height_mm if height_mm != -1.0 else None,
              "type_name": type_name if type_name else None}
    return json.dumps(mcp_event_handler.run_on_main_thread(edit_wall_ui, params))

@mcp.tool()
def move_element(element_id: str, dx_mm: float = 0, dy_mm: float = 0, dz_mm: float = 0, direction: str = "", distance_mm: float = 0) -> str:
    from .bridge import mcp_event_handler
    params = locals()
    return json.dumps(mcp_event_handler.run_on_main_thread(move_element_ui, params))

@mcp.tool()
def create_floor(width_mm: float, length_mm: float, center_x: float = 0, center_y: float = 0, level_name: str = "") -> str:
    """Create a floor. level_name is optional."""
    from .bridge import mcp_event_handler
    params = locals()
    return json.dumps(mcp_event_handler.run_on_main_thread(create_floor_ui, params))

@mcp.tool()
def create_type(category: str, new_name: str, source_type_name: str = "", parameters: dict = None) -> str:
    """Universal tool to create a new type (wall, floor, door, window, column) by duplicating an existing one."""
    from .bridge import mcp_event_handler
    params = {"category": category, "new_name": new_name, 
              "source_type_name": source_type_name if source_type_name else None, 
              "parameters": parameters if parameters else {}}
    return json.dumps(mcp_event_handler.run_on_main_thread(duplicate_family_type_ui, params))

@mcp.tool()
def query_levels() -> str:
    """List all levels in the project."""
    from .bridge import mcp_event_handler
    def action():
        uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
        cl = DB.FilteredElementCollector(doc).OfClass(DB.Level)
        return [{"id": str(lvl.Id.Value), "name": lvl.Name, "elevation": ft_to_mm(lvl.Elevation)} for lvl in cl]
    return json.dumps(mcp_event_handler.run_on_main_thread(action))

def edit_floor_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document
    floor = doc.GetElement(DB.ElementId(int(params['floor_id'])))
    t = DB.Transaction(doc, "MCP: Edit Floor")
    t.Start()
    if params.get('type_name'):
        ft = _find_type_symbol(doc, DB.BuiltInCategory.OST_Floors, params['type_name'])
        if ft: floor.FloorType = ft
    if params.get('offset_mm') is not None:
        param = floor.get_Parameter(DB.BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
        if param: param.Set(mm_to_ft(float(params['offset_mm'])))
    t.Commit()
    return {"success": True}

@mcp.tool()
def edit_floor(floor_id: str, type_name: str = "", offset_mm: float = -1.0) -> str:
    """Edit floor properties. Use -1 for offset or empty string for type to keep current."""
    from .bridge import mcp_event_handler
    params = {"floor_id": floor_id, 
              "type_name": type_name if type_name else None, 
              "offset_mm": offset_mm if offset_mm != -1.0 else None}
    return json.dumps(mcp_event_handler.run_on_main_thread(edit_floor_ui, params))

def list_elements_ui(category):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    cl = DB.FilteredElementCollector(doc)
    if "wall" in category.lower(): cl.OfClass(DB.Wall)
    elif "floor" in category.lower(): cl.OfClass(DB.Floor)
    elif "level" in category.lower(): cl.OfClass(DB.Level)
    elif "grid" in category.lower(): cl.OfClass(DB.Grid)
    elif "door" in category.lower(): cl.OfCategory(DB.BuiltInCategory.OST_Doors).OfClass(DB.FamilyInstance)
    elif "window" in category.lower(): cl.OfCategory(DB.BuiltInCategory.OST_Windows).OfClass(DB.FamilyInstance)
    elif "column" in category.lower(): cl.OfCategory(DB.BuiltInCategory.OST_Columns).OfClass(DB.FamilyInstance)
    
    results = []
    for el in cl:
        bb = el.get_BoundingBox(None)
        loc_str = "None"
        if bb:
            loc_str = "Min:{},{},{} Max:{},{},{}".format(
                int(ft_to_mm(bb.Min.X)), int(ft_to_mm(bb.Min.Y)), int(ft_to_mm(bb.Min.Z)),
                int(ft_to_mm(bb.Max.X)), int(ft_to_mm(bb.Max.Y)), int(ft_to_mm(bb.Max.Z))
            )
        results.append({"id": str(el.Id.Value), "name": el.Name, "location": loc_str})
    return results

def list_family_types_ui(category_name):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    
    cats = {
        "door": DB.BuiltInCategory.OST_Doors,
        "window": DB.BuiltInCategory.OST_Windows,
        "column": DB.BuiltInCategory.OST_Columns,
        "structural_column": DB.BuiltInCategory.OST_StructuralColumns
    }
    
    bip = cats.get(category_name.lower())
    if not bip: return {"error": "Category not supported yet"}
    
    cl = DB.FilteredElementCollector(doc).OfCategory(bip).OfClass(DB.FamilySymbol)
    return [{"id": str(s.Id.Value), "name": s.Name, "family": s.Family.Name} for s in cl]

@mcp.tool()
def list_family_types(category: str) -> str:
    """List available family types for 'door', 'window', or 'column'."""
    from .bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(list_family_types_ui, category))

@mcp.tool()
def list_elements(category: str) -> str:
    """List all elements in a category (wall, floor, level) with their IDs and locations."""
    from .bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(list_elements_ui, category))

def set_parameter_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    try:
        el = doc.GetElement(DB.ElementId(int(params['element_id'])))
        p_name = params['parameter_name']
        val = params['value']
        
        t = DB.Transaction(doc, "MCP: Set Param")
        t.Start()
        
        # Try BuiltIn first
        bip = get_bip(p_name)
        param = el.get_Parameter(bip) if bip else el.LookupParameter(p_name)
        
        if not param: return {"error": "Parameter not found"}
        
        if param.StorageType == DB.StorageType.Double: param.Set(float(val))
        elif param.StorageType == DB.StorageType.Integer: param.Set(int(val))
        elif param.StorageType == DB.StorageType.String: param.Set(str(val))
        elif param.StorageType == DB.StorageType.ElementId: param.Set(DB.ElementId(int(val)))
        
        t.Commit()
        return {"success": True}
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
def set_parameter(element_id: str, parameter_name: str, value: str) -> str:
    """Set any parameter (BuiltIn name or Display name) on an element."""
    from .bridge import mcp_event_handler
    params = locals()
    return json.dumps(mcp_event_handler.run_on_main_thread(set_parameter_ui, params))

@mcp.tool()
def get_parameters(element_id: str) -> str:
    """Read all parameters (BuiltIn and custom) from an element with their current values."""
    from .bridge import mcp_event_handler
    def action():
        import Autodesk.Revit.DB as DB # type: ignore
        uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
        try:
            el = doc.GetElement(DB.ElementId(int(element_id)))
            params = []
            for p in el.Parameters:
                val = "None"
                if p.StorageType == DB.StorageType.Double: val = str(ft_to_mm(p.AsDouble())) if p.Definition.UnitType == DB.UnitType.UT_Length else str(p.AsDouble())
                elif p.StorageType == DB.StorageType.Integer: val = str(p.AsInteger())
                elif p.StorageType == DB.StorageType.String: val = p.AsString()
                elif p.StorageType == DB.StorageType.ElementId: val = str(p.AsElementId().Value)
                
                params.append({"name": p.Definition.Name, "value": val, "id": str(p.Id.Value)})
            return params
        except Exception as e:
            return {"error": str(e)}
    return json.dumps(mcp_event_handler.run_on_main_thread(action))

def query_types_ui(category):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    cl = DB.FilteredElementCollector(doc)
    if "wall" in category.lower(): cl.OfClass(DB.WallType)
    elif "floor" in category.lower(): cl.OfClass(DB.FloorType)
    elif "door" in category.lower(): cl.OfCategory(DB.BuiltInCategory.OST_Doors).OfClass(DB.FamilySymbol)
    elif "window" in category.lower(): cl.OfCategory(DB.BuiltInCategory.OST_Windows).OfClass(DB.FamilySymbol)
    elif "column" in category.lower(): cl.OfCategory(DB.BuiltInCategory.OST_Columns).OfClass(DB.FamilySymbol)
    return [{"id": str(t.Id.Value), "name": t.Name} for t in cl]

@mcp.tool()
def query_types(category: str) -> str:
    """List available types for 'wall' or 'floor'."""
    from .bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(query_types_ui, category))

def create_polygon_floor_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    import System.Collections.Generic as Generic # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    try:
        points = params['points_mm']
        loop = DB.CurveLoop()
        for i in range(len(points)):
            p1 = points[i]
            p2 = points[(i+1)%len(points)]
            loop.Append(DB.Line.CreateBound(DB.XYZ(mm_to_ft(p1['x']), mm_to_ft(p1['y']), 0), 
                                          DB.XYZ(mm_to_ft(p2['x']), mm_to_ft(p2['y']), 0)))
        
        loops = Generic.List[DB.CurveLoop]()
        loops.Add(loop)
        lvl = _find_level(doc, params.get('level_name') or params.get('level_id'))
        ftype = DB.FilteredElementCollector(doc).OfClass(DB.FloorType).FirstElement()
        
        t = DB.Transaction(doc, "MCP: Poly Floor")
        t.Start()
        nf = DB.Floor.Create(doc, loops, ftype.Id, lvl.Id)
        t.Commit()
        return {"success": True, "id": str(nf.Id.Value)}
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
def create_polygon_floor(points_mm: list, level_name: str = "") -> str:
    """Create a floor from a list of points. level_name is optional."""
    from .bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(create_polygon_floor_ui, {"points_mm": points_mm, "level_name": level_name}))

@mcp.tool()
def delete_element(element_id: str) -> str:
    """Delete a specific element by ID."""
    from .bridge import mcp_event_handler
    def action():
        import Autodesk.Revit.DB as DB # type: ignore
        uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
        t = DB.Transaction(doc, "MCP: Delete")
        t.Start()
        doc.Delete(DB.ElementId(int(element_id)))
        t.Commit()
        return {"success": True}
    return json.dumps(mcp_event_handler.run_on_main_thread(action))

def create_level_ui(elevation_mm):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    t = DB.Transaction(doc, "MCP: Level")
    t.Start()
    lvl = DB.Level.Create(doc, mm_to_ft(elevation_mm))
    t.Commit()
    return {"id": str(lvl.Id.Value), "name": lvl.Name}

@mcp.tool()
def create_level(elevation_mm: float) -> str:
    """Create a new level at the specified elevation."""
    from .bridge import mcp_event_handler
    return json.dumps(mcp_event_handler.run_on_main_thread(create_level_ui, elevation_mm))

def create_grid_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    p1 = DB.XYZ(mm_to_ft(params['x1']), mm_to_ft(params['y1']), 0)
    p2 = DB.XYZ(mm_to_ft(params['x2']), mm_to_ft(params['y2']), 0)
    line = DB.Line.CreateBound(p1, p2)
    t = DB.Transaction(doc, "MCP: Grid")
    t.Start()
    grid = DB.Grid.Create(doc, line)
    t.Commit()
    return {"id": str(grid.Id.Value), "name": grid.Name}

@mcp.tool()
def create_grid(x1: float, y1: float, x2: float, y2: float) -> str:
    """Create a grid line between two X/Y points."""
    from .bridge import mcp_event_handler
    params = locals()
    return json.dumps(mcp_event_handler.run_on_main_thread(create_grid_ui, params))

def create_arc_grid_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    p1 = DB.XYZ(mm_to_ft(params['start_x']), mm_to_ft(params['start_y']), 0)
    p2 = DB.XYZ(mm_to_ft(params['end_x']), mm_to_ft(params['end_y']), 0)
    pm = DB.XYZ(mm_to_ft(params['mid_x']), mm_to_ft(params['mid_y']), 0)
    arc = DB.Arc.Create(p1, p2, pm)
    t = DB.Transaction(doc, "MCP: Arc Grid")
    t.Start()
    grid = DB.Grid.Create(doc, arc)
    t.Commit()
    return {"id": str(grid.Id.Value), "name": grid.Name}

@mcp.tool()
def create_arc_grid(start_x: float, start_y: float, end_x: float, end_y: float, mid_x: float, mid_y: float) -> str:
    """Create a curved (arc) grid using start, end, and a point on the arc."""
    from .bridge import mcp_event_handler
    params = locals()
    return json.dumps(mcp_event_handler.run_on_main_thread(create_arc_grid_ui, params))

def edit_grid_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    grid = doc.GetElement(DB.ElementId(int(params['grid_id'])))
    t = DB.Transaction(doc, "MCP: Edit Grid")
    t.Start()
    if 'name' in params:
        grid.Name = params['name']
    if 'type_name' in params:
        types = DB.FilteredElementCollector(doc).OfClass(DB.GridType)
        for gt in types:
            if params['type_name'].lower() in gt.Name.lower():
                grid.GridType = gt
                break
    t.Commit()
    return {"success": True}

@mcp.tool()
def edit_grid(grid_id: str, name: str = None, type_name: str = None) -> str:
    """Rename or change the type of a grid."""
    from .bridge import mcp_event_handler
    params = locals()
    return json.dumps(mcp_event_handler.run_on_main_thread(edit_grid_ui, params))

@mcp.tool()
def create_column(type_name: str = "", level_name: str = "", x: float = 0, y: float = 0, rotation_degrees: float = 0) -> str:
    """Create an architectural column. x/y in mm."""
    from .bridge import mcp_event_handler
    params = {"type_name": type_name if type_name else None, "level_name": level_name if level_name else None, "x": x, "y": y, "rotation_degrees": rotation_degrees}
    return json.dumps(mcp_event_handler.run_on_main_thread(create_column_ui, params))

@mcp.tool()
def create_door(wall_id: str, type_name: str = "", offset_mm: float = 1000) -> str:
    """Place a door in a wall. offset_mm is distance from wall start."""
    from .bridge import mcp_event_handler
    params = {"wall_id": wall_id, "type_name": type_name if type_name else None, "offset_mm": offset_mm, "category": "door"}
    return json.dumps(mcp_event_handler.run_on_main_thread(create_hosted_ui, params))

@mcp.tool()
def create_window(wall_id: str, type_name: str = "", offset_mm: float = 1000, sill_height_mm: float = 900) -> str:
    """Place a window in a wall."""
    from .bridge import mcp_event_handler
    params = {"wall_id": wall_id, "type_name": type_name if type_name else None, "offset_mm": offset_mm, "sill_height_mm": sill_height_mm, "category": "window"}
    return json.dumps(mcp_event_handler.run_on_main_thread(create_hosted_ui, params))

@mcp.tool()
def edit_column(column_id: str, type_name: str = "", x: float = -1.0, y: float = -1.0, rotation_degrees: float = -1.0) -> str:
    """Edit column location, type, or rotation. Use -1 for x/y/rotation to keep current value."""
    from .bridge import mcp_event_handler
    # Convert -1 to None for internal logic
    params = {"column_id": column_id, "type_name": type_name if type_name else None, 
              "x": x if x != -1.0 else None, "y": y if y != -1.0 else None, 
              "rotation_degrees": rotation_degrees if rotation_degrees != -1.0 else None}
    return json.dumps(mcp_event_handler.run_on_main_thread(edit_column_ui, params))

@mcp.tool()
def edit_hosted_element(element_id: str, type_name: str = "", offset_mm: float = -1.0, sill_height_mm: float = -1.0) -> str:
    """Edit door or window placement (offset from host start) or type/sill-height. Use -1 to keep current."""
    from .bridge import mcp_event_handler
    params = {"element_id": element_id, "type_name": type_name if type_name else None, 
              "offset_mm": offset_mm if offset_mm != -1.0 else None, 
              "sill_height_mm": sill_height_mm if sill_height_mm != -1.0 else None}
    return json.dumps(mcp_event_handler.run_on_main_thread(edit_hosted_element_ui, params))

@mcp.tool()
def duplicate_family_type(category: str, new_name: str, source_type_name: str = "", parameters: dict = None) -> str:
    """Create a new type for 'door', 'window', or 'column' by duplicating an existing one."""
    from .bridge import mcp_event_handler
    params = {"category": category, "new_name": new_name, 
              "source_type_name": source_type_name if source_type_name else None, 
              "parameters": parameters if parameters else {}}
    return json.dumps(mcp_event_handler.run_on_main_thread(duplicate_family_type_ui, params))

@mcp.tool()
def place_family_instance(type_name: str, level_name: str = "", x: float = 0, y: float = 0, z: float = 0, rotation: float = 0, parameters: dict = None) -> str:
    """Place any non-hosted family (furniture, equipment, etc.) by type name."""
    from .bridge import mcp_event_handler
    params = locals()
    return json.dumps(mcp_event_handler.run_on_main_thread(place_family_instance_ui, params))

@mcp.tool()
def edit_element(element_id: str, type_name: str = "", x: float = -1.0, y: float = -1.0, z: float = -1.0, rotation_degrees: float = -1.0, parameters: dict = None) -> str:
    """Edit any element's parameters, type, or location. Use -1 for x/y/z/rotation to keep current."""
    from .bridge import mcp_event_handler
    params = {"element_id": element_id, "type_name": type_name if type_name else None,
              "x": x if x != -1.0 else None, "y": y if y != -1.0 else None, "z": z if z != -1.0 else None,
              "rotation_degrees": rotation_degrees if rotation_degrees != -1.0 else None,
              "parameters": parameters if parameters else {}}
    return json.dumps(mcp_event_handler.run_on_main_thread(edit_element_ui, params))

@mcp.tool()
def edit_type(type_name: str = "", type_id: str = "", parameters: dict = None) -> str:
    """Edit a family type's parameters project-wide."""
    from .bridge import mcp_event_handler
    params = {"type_name": type_name, "type_id": type_id if type_id else None, "parameters": parameters if parameters else {}}
    return json.dumps(mcp_event_handler.run_on_main_thread(edit_type_ui, params))
