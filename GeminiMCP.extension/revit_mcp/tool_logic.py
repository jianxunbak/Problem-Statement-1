# -*- coding: utf-8 -*-
import json
import time
import math
from revit_mcp.state_manager import state_manager
from revit_mcp.utils import (
    mm_to_ft, ft_to_mm, get_bip, 
    find_level, find_type_symbol, set_params_batch
)

# Application access (initialized by server.py)
_get_revit_app = None

def initialize(get_app_func):
    global _get_revit_app
    _get_revit_app = get_app_func

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
    
    sx = params.get('start_x', 0)
    sy = params.get('start_y', 0)
    p1 = DB.XYZ(mm_to_ft(sx), mm_to_ft(sy), 0)
    
    ex = params.get('end_x')
    ey = params.get('end_y')
    
    if ex is not None and ey is not None:
        p2 = DB.XYZ(mm_to_ft(ex), mm_to_ft(ey), 0)
    else:
        l_mm = params.get('length_mm', 5000)
        p2 = DB.XYZ(mm_to_ft(sx + l_mm), mm_to_ft(sy), 0)
    
    try:
        line = DB.Line.CreateBound(p1, p2)
    except Exception as e:
        dist_ft = p1.DistanceTo(p2)
        msg = "Failed to create wall curve: {}. Target length: {}mm. Revit min is ~1.6mm.".format(str(e), ft_to_mm(dist_ft))
        return {"error": msg}
    
    level = find_level(doc, params.get('level_name') or params.get('level_id'))
    
    t = DB.Transaction(doc, "MCP: Create Wall")
    t.Start()
    new_wall = DB.Wall.Create(doc, line, level.Id, False)
    state_manager.set_ai_metadata(new_wall, params.get('ai_id') or "AI_Wall_{}".format(new_wall.Id.Value))
    
    param = new_wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
    if param: param.Set(mm_to_ft(params.get('height_mm', 3000)))
    
    thickness_mm = params.get('thickness_mm')
    if thickness_mm:
        wt = next((w for w in DB.FilteredElementCollector(doc).OfClass(DB.WallType) if str(int(thickness_mm)) in w.Name), None)
        if wt: new_wall.WallType = wt
                
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
        
        if isinstance(el, DB.Grid):
            c = el.Curve
            info["geometry"].append({"type": "grid_line", "start": {"x": ft_to_mm(c.GetEndPoint(0).X), "y": ft_to_mm(c.GetEndPoint(0).Y)}, "end": {"x": ft_to_mm(c.GetEndPoint(1).X), "y": ft_to_mm(c.GetEndPoint(1).Y)}})
        elif isinstance(el, DB.Level):
            info["elevation_mm"] = ft_to_mm(el.Elevation)

        loc = el.Location
        if loc and hasattr(loc, "Curve") and isinstance(loc.Curve, DB.Line):
            c = loc.Curve
            info["geometry"].append({"type": "line", "start": {"x": ft_to_mm(c.GetEndPoint(0).X), "y": ft_to_mm(c.GetEndPoint(0).Y)}, "end": {"x": ft_to_mm(c.GetEndPoint(1).X), "y": ft_to_mm(c.GetEndPoint(1).Y)}})
        elif loc and hasattr(loc, "Point"):
            p = loc.Point
            info["point"] = {"x": ft_to_mm(p.X), "y": ft_to_mm(p.Y), "z": ft_to_mm(p.Z)}

        if isinstance(el, DB.Floor):
            seen_lines = set()
            for g in el.get_Geometry(DB.Options()):
                if isinstance(g, DB.Solid):
                    for edge in g.Edges:
                        c = edge.AsCurve()
                        if isinstance(c, DB.Line):
                            p1, p2 = c.GetEndPoint(0), c.GetEndPoint(1)
                            if abs(p1.X - p2.X) < 0.001 and abs(p1.Y - p2.Y) < 0.001: continue
                            line_key = tuple(sorted([(round(p1.X,3), round(p1.Y,3)), (round(p2.X,3), round(p2.Y,3))]))
                            if line_key in seen_lines: continue
                            seen_lines.add(line_key)
                            info["geometry"].append({"type": "line", "start": {"x": ft_to_mm(p1.X), "y": ft_to_mm(p1.Y)}, "end": {"x": ft_to_mm(p2.X), "y": ft_to_mm(p2.Y)}})
        return info
    except Exception as e: return {"error": str(e)}

def delete_walls_ui():
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    walls = DB.FilteredElementCollector(doc).OfClass(DB.Wall).ToElementIds()
    if walls.Count == 0: return {"success": True, "message": "No walls."}
    t = DB.Transaction(doc, "MCP: Delete Walls"); t.Start(); doc.Delete(walls); t.Commit()
    return {"success": True}

def edit_wall_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    wall = doc.GetElement(DB.ElementId(int(params['wall_id'])))
    t = DB.Transaction(doc, "MCP: Edit Wall"); t.Start()
    if params.get('type_name'):
        wt = find_type_symbol(doc, DB.BuiltInCategory.OST_Walls, params['type_name'])
        if wt: wall.WallType = wt
    if params.get('height_mm') is not None:
        p = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
        if p: p.Set(mm_to_ft(float(params['height_mm'])))
    if params.get('length_mm') is not None:
        lc = wall.Location
        if hasattr(lc, "Curve") and isinstance(lc.Curve, DB.Line):
            old_line = lc.Curve
            new_end = old_line.GetEndPoint(0) + old_line.Direction.Normalize() * mm_to_ft(float(params['length_mm']))
            try: lc.Curve = DB.Line.CreateBound(old_line.GetEndPoint(0), new_end)
            except Exception as e: return {"error": "Failed to resize: " + str(e)}
    t.Commit()
    return {"success": True}

def move_element_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    eid = DB.ElementId(int(params['element_id']))
    dx, dy, dz = params.get('dx_mm', 0), params.get('dy_mm', 0), params.get('dz_mm', 0)
    direction, dist = params.get('direction', '').lower(), params.get('distance_mm', 0)
    if direction == 'north': dy += dist
    elif direction == 'south': dy -= dist
    elif direction == 'east': dx += dist
    elif direction == 'west': dx -= dist
    t = DB.Transaction(doc, "MCP: Move"); t.Start()
    DB.ElementTransformUtils.MoveElement(doc, eid, DB.XYZ(mm_to_ft(dx), mm_to_ft(dy), mm_to_ft(dz)))
    t.Commit()
    return {"success": True}

def create_floor_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    import System.Collections.Generic as Generic # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    w, l = mm_to_ft(params.get('width_mm', 5000)), mm_to_ft(params.get('length_mm', 5000))
    x, y = mm_to_ft(params.get('center_x', 0)), mm_to_ft(params.get('center_y', 0))
    try:
        p1, p2, p3, p4 = DB.XYZ(x-w/2, y-l/2, 0), DB.XYZ(x+w/2, y-l/2, 0), DB.XYZ(x+w/2, y+l/2, 0), DB.XYZ(x-w/2, y+l/2, 0)
        profile = DB.CurveLoop()
        profile.Append(DB.Line.CreateBound(p1, p2)); profile.Append(DB.Line.CreateBound(p2, p3))
        profile.Append(DB.Line.CreateBound(p3, p4)); profile.Append(DB.Line.CreateBound(p4, p1))
        loops = Generic.List[DB.CurveLoop](); loops.Add(profile)
        lvl = find_level(doc, params.get('level_name') or params.get('level_id'))
        ftype = DB.FilteredElementCollector(doc).OfClass(DB.FloorType).FirstElement()
        t = DB.Transaction(doc, "MCP: Create Floor"); t.Start()
        new_floor = DB.Floor.Create(doc, loops, ftype.Id, lvl.Id)
        state_manager.set_ai_metadata(new_floor, params.get('ai_id') or "AI_Floor_{}".format(new_floor.Id.Value))
        t.Commit()
        return {"success": True, "floor_id": str(new_floor.Id.Value)}
    except Exception as e: return {"error": str(e)}

def create_column_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    level, symbol = find_level(doc, params.get('level_name')), find_type_symbol(doc, DB.BuiltInCategory.OST_Columns, params.get('type_name'))
    if not symbol.IsActive:
        t = DB.Transaction(doc, "Load symbol"); t.Start(); symbol.Activate(); t.Commit()
    p = DB.XYZ(mm_to_ft(params.get('x', 0)), mm_to_ft(params.get('y', 0)), level.Elevation)
    t = DB.Transaction(doc, "MCP: Column"); t.Start()
    inst = doc.Create.NewFamilyInstance(p, symbol, level, DB.Structure.StructuralType.NonStructural)
    rot = params.get('rotation_degrees', 0)
    if rot != 0: DB.ElementTransformUtils.RotateElement(doc, inst.Id, DB.Line.CreateBound(p, p+DB.XYZ.BasisZ), rot * (math.pi / 180.0))
    t.Commit()
    return {"success": True, "id": str(inst.Id.Value)}

def create_hosted_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    category = DB.BuiltInCategory.OST_Doors if params['category'] == 'door' else DB.BuiltInCategory.OST_Windows
    symbol, wall = find_type_symbol(doc, category, params.get('type_name')), doc.GetElement(DB.ElementId(int(params['wall_id'])))
    if not symbol.IsActive:
        t = DB.Transaction(doc, "Activate"); t.Start(); symbol.Activate(); t.Commit()
    line = wall.Location.Curve
    p = line.GetEndPoint(0) + line.Direction.Normalize() * mm_to_ft(params.get('offset_mm', 1000))
    t = DB.Transaction(doc, "MCP: Hosted"); t.Start()
    inst = doc.Create.NewFamilyInstance(p, symbol, wall, doc.GetElement(wall.LevelId), DB.Structure.StructuralType.NonStructural)
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
    t = DB.Transaction(doc, "MCP: Edit Column"); t.Start()
    if params.get('type_name'):
        symbol = find_type_symbol(doc, DB.BuiltInCategory.OST_Columns, params['type_name'])
        if symbol: inst.Symbol = symbol
    if params.get('x') is not None or params.get('y') is not None:
        loc = inst.Location
        old_p = loc.Point
        new_x, new_y = mm_to_ft(params.get('x', old_p.X*304.8)), mm_to_ft(params.get('y', old_p.Y*304.8))
        loc.Point = DB.XYZ(new_x, new_y, old_p.Z)
    if params.get('rotation_degrees') is not None:
        p = inst.Location.Point
        DB.ElementTransformUtils.RotateElement(doc, inst.Id, DB.Line.CreateBound(p, p+DB.XYZ.BasisZ), params['rotation_degrees']*(math.pi/180.0))
    t.Commit()
    return {"success": True}

def edit_hosted_element_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    inst = doc.GetElement(DB.ElementId(int(params['element_id'])))
    t = DB.Transaction(doc, "MCP: Edit Hosted"); t.Start()
    if params.get('type_name'):
        symbol = find_type_symbol(doc, inst.Category.BuiltInCategory, params['type_name'])
        if symbol: inst.Symbol = symbol
    if params.get('offset_mm') is not None and inst.Host:
        line = inst.Host.Location.Curve
        p_new = line.GetEndPoint(0) + line.Direction.Normalize()*mm_to_ft(params['offset_mm'])
        DB.ElementTransformUtils.MoveElement(doc, inst.Id, p_new - inst.Location.Point)
    if params.get('sill_height_mm') is not None:
        p_sill = inst.get_Parameter(DB.BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
        if p_sill: p_sill.Set(mm_to_ft(params['sill_height_mm']))
    t.Commit()
    return {"success": True}

def duplicate_family_type_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    cat_map = {"door": DB.BuiltInCategory.OST_Doors, "window": DB.BuiltInCategory.OST_Windows, "column": DB.BuiltInCategory.OST_Columns, "wall": DB.BuiltInCategory.OST_Walls, "floor": DB.BuiltInCategory.OST_Floors}
    source = find_type_symbol(doc, cat_map.get(params['category'].lower(), DB.BuiltInCategory.OST_Doors), params.get('source_type_name'))
    if not source: return {"error": "Source not found"}
    t = DB.Transaction(doc, "MCP: Create Type"); t.Start()
    new_type = source.Duplicate(params['new_name'])
    p_dict = params.get('parameters', {})
    if params['category'].lower() in ["wall", "floor"] and "thickness_mm" in p_dict:
        cs = new_type.GetCompoundStructure()
        if cs:
            layers = cs.GetLayers()
            for i in range(layers.Count):
                if layers[i].Function == DB.MaterialFunctionAssignment.Structure:
                    layers[i].Width = mm_to_ft(float(p_dict["thickness_mm"])); break
            cs.SetLayers(layers); new_type.SetCompoundStructure(cs)
    set_params_batch(new_type, p_dict)
    t.Commit()
    return {"success": True, "id": str(new_type.Id.Value)}

def place_family_instance_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    symbol = next((s for s in DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol) if s.Name.lower() == params['type_name'].lower()), None)
    if not symbol: return {"error": "Symbol not found"}
    level = find_level(doc, params.get('level_name'))
    p = DB.XYZ(mm_to_ft(params.get('x', 0)), mm_to_ft(params.get('y', 0)), mm_to_ft(params.get('z', 0)) + level.Elevation)
    t = DB.Transaction(doc, "MCP: Place Family"); t.Start()
    if not symbol.IsActive: symbol.Activate()
    inst = doc.Create.NewFamilyInstance(p, symbol, level, DB.Structure.StructuralType.NonStructural)
    rot = params.get('rotation', 0)
    if rot != 0: DB.ElementTransformUtils.RotateElement(doc, inst.Id, DB.Line.CreateBound(p, p+DB.XYZ.BasisZ), rot * (math.pi / 180.0))
    set_params_batch(inst, params.get('parameters', {}))
    t.Commit()
    return {"success": True, "id": str(inst.Id.Value)}

def edit_element_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    el = doc.GetElement(DB.ElementId(int(params['element_id'])))
    t = DB.Transaction(doc, "MCP: Edit Element"); t.Start()
    if params.get('type_name'):
        new_sym = find_type_symbol(doc, el.Category.BuiltInCategory, params['type_name'])
        if new_sym:
            if hasattr(el, 'Symbol'): el.Symbol = new_sym
            elif isinstance(el, DB.Wall): el.WallType = new_sym
            elif isinstance(el, DB.Floor): el.FloorType = new_sym
    if any(params.get(p) is not None for p in ["x", "y", "z"]) and hasattr(el.Location, 'Point'):
        p = el.Location.Point
        el.Location.Point = DB.XYZ(mm_to_ft(params.get('x', p.X*304.8)), mm_to_ft(params.get('y', p.Y*304.8)), mm_to_ft(params.get('z', p.Z*304.8)))
    if params.get('rotation_degrees') is not None:
        p = el.Location.Point if hasattr(el.Location, 'Point') else DB.XYZ.Zero
        DB.ElementTransformUtils.RotateElement(doc, el.Id, DB.Line.CreateBound(p, p+DB.XYZ.BasisZ), params['rotation_degrees'] * (math.pi / 180.0))
    set_params_batch(el, params.get('parameters', {}))
    t.Commit()
    return {"success": True}

def edit_type_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    etype = doc.GetElement(DB.ElementId(int(params['type_id']))) if params.get('type_id') else next((t for t in DB.FilteredElementCollector(doc).OfClass(DB.ElementType) if params['type_name'].lower() in t.Name.lower()), None)
    if not etype: return {"error": "Type not found"}
    t = DB.Transaction(doc, "MCP: Edit Type"); t.Start(); set_params_batch(etype, params.get('parameters', {})); t.Commit()
    return {"success": True}

def get_building_metrics_ui():
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    lvls = sorted([l for l in DB.FilteredElementCollector(doc).OfClass(DB.Level) if "AI" in l.Name], key=lambda x: x.Elevation)
    if not lvls: return 0, 0, True
    count = len(lvls)
    if count < 2: return count, 0, True
    h = DB.UnitUtils.ConvertFromInternalUnits(lvls[1].Elevation - lvls[0].Elevation, DB.UnitTypeId.Millimeters)
    consistent = all(abs(DB.UnitUtils.ConvertFromInternalUnits(lvls[i].Elevation - lvls[i-1].Elevation, DB.UnitTypeId.Millimeters) - h) < 1.0 for i in range(1, count))
    return count, h, consistent

def list_elements_ui(category):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    cl = DB.FilteredElementCollector(doc)
    cat = category.lower()
    if "wall" in cat: cl.OfClass(DB.Wall)
    elif "floor" in cat: cl.OfClass(DB.Floor)
    elif "level" in cat: cl.OfClass(DB.Level)
    elif "grid" in cat: cl.OfClass(DB.Grid)
    elif "door" in cat: cl.OfCategory(DB.BuiltInCategory.OST_Doors).OfClass(DB.FamilyInstance)
    elif "window" in cat: cl.OfCategory(DB.BuiltInCategory.OST_Windows).OfClass(DB.FamilyInstance)
    elif "column" in cat: cl.OfCategory(DB.BuiltInCategory.OST_Columns).OfClass(DB.FamilyInstance)
    return [{"id": str(el.Id.Value), "name": el.Name} for el in cl]

def query_types_ui(category):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    cl = DB.FilteredElementCollector(doc)
    cat = category.lower()
    if "wall" in cat: cl.OfClass(DB.WallType)
    elif "floor" in cat: cl.OfClass(DB.FloorType)
    elif "door" in cat: cl.OfCategory(DB.BuiltInCategory.OST_Doors).OfClass(DB.FamilySymbol)
    elif "window" in cat: cl.OfCategory(DB.BuiltInCategory.OST_Windows).OfClass(DB.FamilySymbol)
    elif "column" in cat: cl.OfCategory(DB.BuiltInCategory.OST_Columns).OfClass(DB.FamilySymbol)
    return [{"id": str(t.Id.Value), "name": t.Name} for t in cl]

def create_polygon_floor_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    import System.Collections.Generic as Generic # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    points = params['points_mm']
    loop = DB.CurveLoop()
    for i in range(len(points)):
        p1, p2 = points[i], points[(i+1)%len(points)]
        loop.Append(DB.Line.CreateBound(DB.XYZ(mm_to_ft(p1['x']), mm_to_ft(p1['y']), 0), DB.XYZ(mm_to_ft(p2['x']), mm_to_ft(p2['y']), 0)))
    loops = Generic.List[DB.CurveLoop](); loops.Add(loop)
    lvl = find_level(doc, params.get('level_name') or params.get('level_id'))
    ftype = DB.FilteredElementCollector(doc).OfClass(DB.FloorType).FirstElement()
    t = DB.Transaction(doc, "MCP: Poly Floor"); t.Start(); nf = DB.Floor.Create(doc, loops, ftype.Id, lvl.Id); t.Commit()
    return {"success": True, "id": str(nf.Id.Value)}

def create_level_ui(elevation_mm):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    t = DB.Transaction(doc, "MCP: Level"); t.Start(); lvl = DB.Level.Create(doc, mm_to_ft(elevation_mm)); t.Commit()
    return {"id": str(lvl.Id.Value), "name": lvl.Name}

def create_grid_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    p1, p2 = DB.XYZ(mm_to_ft(params['x1']), mm_to_ft(params['y1']), 0), DB.XYZ(mm_to_ft(params['x2']), mm_to_ft(params['y2']), 0)
    line = DB.Line.CreateBound(p1, p2)
    t = DB.Transaction(doc, "MCP: Grid"); t.Start(); grid = DB.Grid.Create(doc, line); t.Commit()
    return {"id": str(grid.Id.Value), "name": grid.Name}

def create_arc_grid_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    p1, p2, pm = DB.XYZ(mm_to_ft(params['start_x']), mm_to_ft(params['start_y']), 0), DB.XYZ(mm_to_ft(params['end_x']), mm_to_ft(params['end_y']), 0), DB.XYZ(mm_to_ft(params['mid_x']), mm_to_ft(params['mid_y']), 0)
    t = DB.Transaction(doc, "MCP: Arc Grid"); t.Start(); grid = DB.Grid.Create(doc, DB.Arc.Create(p1, p2, pm)); t.Commit()
    return {"id": str(grid.Id.Value), "name": grid.Name}

def edit_grid_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    grid = doc.GetElement(DB.ElementId(int(params['grid_id'])))
    t = DB.Transaction(doc, "MCP: Edit Grid"); t.Start()
    if 'name' in params: grid.Name = params['name']
    if 'type_name' in params:
        gt = next((g for g in DB.FilteredElementCollector(doc).OfClass(DB.GridType) if params['type_name'].lower() in g.Name.lower()), None)
        if gt: grid.GridType = gt
    t.Commit()
    return {"success": True}

def set_parameter_ui(params):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app(); doc = uiapp.ActiveUIDocument.Document
    el = doc.GetElement(DB.ElementId(int(params['element_id'])))
    t = DB.Transaction(doc, "MCP: Set Param"); t.Start()
    bip = get_bip(params['parameter_name'])
    param = el.get_Parameter(bip) if bip else el.LookupParameter(params['parameter_name'])
    if param:
        val = params['value']
        if param.StorageType == DB.StorageType.Double: param.Set(float(val))
        elif param.StorageType == DB.StorageType.Integer: param.Set(int(val))
        elif param.StorageType == DB.StorageType.String: param.Set(str(val))
    t.Commit()
    return {"success": True}
