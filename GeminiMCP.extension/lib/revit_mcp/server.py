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

def get_doc_info_ui():
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document
    if not doc:
        return {"error": "No active document"}
    return {
        "title": doc.Title,
        "is_family_document": doc.IsFamilyDocument,
        "path": doc.PathName
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

def query_elements_ui(category_name):
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document
    if not doc:
        return {"error": "No active document"}
    
    return {"message": "Queried elements for category: {}".format(category_name), "elements": []}

@mcp.tool()
def query_elements(category_name: str) -> str:
    """Query Revit elements by name or category."""
    from .event_handler import mcp_event_handler
    try:
        result = mcp_event_handler.run_on_main_thread(query_elements_ui, category_name)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})

def read_element_ui(element_id_val):
    import Autodesk.Revit.DB as DB # type: ignore
    import System
    uiapp = _get_revit_app()
    doc = uiapp.ActiveUIDocument.Document
    if not doc:
        return {"error": "No active document"}
        
    try:
        # In Revit 2026, ElementId requires an Int64
        element_id = DB.ElementId(System.Int64(element_id_val))
        element = doc.GetElement(element_id)
        if not element:
            return {"error": "Element {} not found".format(element_id_val)}
        return {"success": True, "name": element.Name, "id": element_id_val}
    except Exception as e:
        return {"error": str(e)}

@mcp.tool()
def read_element(element_id: int) -> str:
    """Read specific element details using its Int64 ID in Revit 2026."""
    from .event_handler import mcp_event_handler
    try:
        result = mcp_event_handler.run_on_main_thread(read_element_ui, element_id)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})

def create_wall_ui(length_ft):
    import Autodesk.Revit.DB as DB # type: ignore
    uiapp = _get_revit_app()
    if not uiapp:
        return {"error": "No Revit application available (HOST_APP.uiapp is None)."}
    uidoc = uiapp.ActiveUIDocument
    if not uidoc:
        return {"error": "No active document open in Revit. Please open a project file first."}
    doc = uidoc.Document
    if not doc:
        return {"error": "Active document is null."}
        
    try:
        # 1. Find the first Level in the document
        collector = DB.FilteredElementCollector(doc).OfClass(DB.Level)
        level = collector.FirstElement()
        if not level:
            return {"error": "No level found in document"}
            
        # 2. Define a Line (coordinates in feet)
        p1 = DB.XYZ(0, 0, 0)
        p2 = DB.XYZ(length_ft, 0, 0)
        line = DB.Line.CreateBound(p1, p2)
        
        # 3. Create Wall inside a Transaction
        # Use explicit Start/Commit - Python.NET does not support 'with' for DB.Transaction
        t = DB.Transaction(doc, "MCP: Create Wall")
        t.Start()
        try:
            new_wall = DB.Wall.Create(doc, line, level.Id, False)
            t.Commit()
        except Exception as tx_err:
            t.RollbackToBeforeStart()
            return {"error": "Transaction failed: " + str(tx_err)}
            
        return {
            "success": True, 
            "message": "Wall created successfully!",
            "wall_id": str(new_wall.Id.Value),
            "length": length_ft,
            "level": level.Name
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}

@mcp.tool()
def create_wall(length: float = 10.0) -> str:
    """Create a simple wall in the active Revit document (specify length in feet)."""
    from .event_handler import mcp_event_handler
    try:
        result = mcp_event_handler.run_on_main_thread(create_wall_ui, length)
        return json.dumps(result)
    except Exception as e:
        return json.dumps({"error": str(e)})
