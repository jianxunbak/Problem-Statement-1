# -*- coding: utf-8 -*-
import os
import math
import re
import json

def load_presets():
    try:
        preset_path = os.path.join(os.path.dirname(__file__), "building_presets.json")
        if os.path.exists(preset_path):
            with open(preset_path, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

_cached_log_path = None

def get_log_path():
    global _cached_log_path
    if _cached_log_path:
        return _cached_log_path
    # Consolidate to main revit-MCP folder (same level as .env)
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    _cached_log_path = os.path.join(root, "fastmcp_server.log")
    return _cached_log_path

def safe_num(val, default=0):
    try:
        if val is None: return float(default) if default is not None else None
        if isinstance(val, (int, float)): return float(val)
        if isinstance(val, str):
            # Strip non-numeric like 'mm'
            m = re.findall(r"[-+]?\d*\.\d+|\d+", val)
            if m: return float(m[0])
        return float(default) if default is not None else None
    except:
        return float(default) if default is not None else None

def get_random_dim(val, base, variation=0.2):
    """
    If val is 'random', returns a value centered at 'base' within +/- 'variation'%.
    Otherwise returns safe_num(val, base).
    """
    import random
    if isinstance(val, str) and "random" in val.lower():
        v = float(base)
        return v * (1.0 + random.uniform(-variation, variation))
    return safe_num(val, base)

def mm_to_ft(mm):
    import Autodesk.Revit.DB as DB # type: ignore
    return DB.UnitUtils.ConvertToInternalUnits(safe_num(mm), DB.UnitTypeId.Millimeters)

def ft_to_mm(ft):
    import Autodesk.Revit.DB as DB # type: ignore
    return DB.UnitUtils.ConvertFromInternalUnits(float(ft), DB.UnitTypeId.Millimeters)

def sqmm_to_sqft(sqmm):
    import Autodesk.Revit.DB as DB # type: ignore
    return DB.UnitUtils.ConvertToInternalUnits(float(sqmm), DB.UnitTypeId.SquareMillimeters)

def safe_set_comment(element, tag):
    import Autodesk.Revit.DB as DB # type: ignore
    # Try global instance comments first
    p = element.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
    if not p:
        p = element.LookupParameter("Comments")
    if p:
        p.Set(tag)
    return p

def get_bip(name):
    import Autodesk.Revit.DB as DB # type: ignore
    try: return getattr(DB.BuiltInParameter, name)
    except: return None

def find_level(doc, level_id_or_name=None):
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

def find_type_symbol(doc, category_bip, type_name=None):
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

def set_params_batch(element, params_dict):
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

def get_location_line_param(wall):
    import Autodesk.Revit.DB as DB # type: ignore
    bip = getattr(DB.BuiltInParameter, "WALL_KEY_REF_PARAM", None)
    if bip:
        p = wall.get_Parameter(bip)
        if p: return p
    return wall.LookupParameter("Location Line")
