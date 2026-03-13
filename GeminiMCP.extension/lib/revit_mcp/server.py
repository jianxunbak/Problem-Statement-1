# -*- coding: utf-8 -*-
import json
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from fastmcp import FastMCP

# We move CLR imports inside the functions or into a lazy initialization
# to prevent breaking pyRevit's own initialization during script load.

mcp = FastMCP("Revit2026_MCP", debug=True)

def _get_revit_app():
    """Access the global __revit__ object provided by pyRevit."""
    global __revit__
    return __revit__

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
