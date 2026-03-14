# -*- coding: utf-8 -*-
import json
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from fastmcp import FastMCP

# We move CLR imports inside the functions or into a lazy initialization
# to prevent breaking pyRevit's own initialization during script load.

mcp = FastMCP("Revit2026_MCP", debug=False)

# Stored at server startup from the pyRevit UI thread context
_uiapp = None

def set_revit_app(uiapp):
    """Called from runner.py at startup to store UIApplication from the UI thread."""
    global _uiapp
    _uiapp = uiapp

def _get_revit_app():
    """Return the stored UIApplication. Set at startup by set_revit_app()."""
    return _uiapp


# --- TOOLS ---

def get_doc_info_ui():
    """UI thread logic to collect doc info."""
    uiapp = _get_revit_app()
    if not uiapp or not uiapp.ActiveUIDocument:
        return {"error": "No active document."}
    doc = uiapp.ActiveUIDocument.Document
    return {
        "title": doc.Title,
        "is_family_document": doc.IsFamilyDocument,
        "path": doc.PathName or "Unsaved Document"
    }

@mcp.resource("revit://document/info")
def get_document_info() -> str:
    """Get basic information about the active Revit document."""
    from .event_handler import mcp_event_handler
    try:
        result = mcp_event_handler.run_on_main_thread(get_doc_info_ui)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})

def read_element_ui(element_id_val):
    """UI thread logic to read an element."""
    import Autodesk.Revit.DB as DB # type: ignore
    import System
    uiapp = _get_revit_app()
    if not uiapp or not uiapp.ActiveUIDocument:
        return {"error": "No active document."}
    doc = uiapp.ActiveUIDocument.Document
    try:
        element_id = DB.ElementId(System.Int64(element_id_val))
        element = doc.GetElement(element_id)
        if not element:
            return {"error": "Element {} not found".format(element_id_val)}
        return {"success": True, "name": element.Name, "category": element.Category.Name if element.Category else "None"}
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
def read_element(element_id: int) -> str:
    """Read basic details of a Revit element by its Int64 ID."""
    from .event_handler import mcp_event_handler
    try:
        result = mcp_event_handler.run_on_main_thread(read_element_ui, element_id)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})

def create_wall_ui(length_ft):
    """UI thread logic to create a wall."""
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app()
    if not uiapp or not uiapp.ActiveUIDocument:
        return {"error": "No active document."}
    doc = uiapp.ActiveUIDocument.Document
    
    try:
        # Get base level
        level = DB.FilteredElementCollector(doc).OfClass(DB.Level).FirstElement()
        if not level:
            return {"error": "No level found in project."}
            
        line = DB.Line.CreateBound(DB.XYZ(0, 0, 0), DB.XYZ(length_ft, 0, 0))
        
        t = DB.Transaction(doc, "MCP: Create Wall")
        t.Start()
        try:
            new_wall = DB.Wall.Create(doc, line, level.Id, False)
            t.Commit()
            return {
                "success": True, 
                "wall_id": str(new_wall.Id.Value),
                "level": level.Name
            }
        except Exception as tx_err:
            t.RollbackToBeforeStart()
            return {"error": "Transaction failed: " + str(tx_err)}
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
def create_wall(length: float = 10.0) -> str:
    """Create a wall in Revit (length in feet)."""
    from .event_handler import mcp_event_handler
    try:
        result = mcp_event_handler.run_on_main_thread(create_wall_ui, length)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})
