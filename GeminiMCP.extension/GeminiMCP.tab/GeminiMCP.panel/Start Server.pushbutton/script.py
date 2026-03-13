#! python3
# -*- coding: utf-8 -*-
import sys

# 1. IMPORT PYREVIT FIRST (Diagnostic)
try:
    print("Initalizing pyRevit...")
    import pyrevit
    print("pyrevit base imported.")
    from pyrevit import script
    print("pyrevit.script imported.")
    from pyrevit import forms
    print("pyrevit.forms imported.")
    print("pyRevit modules loaded successfully.")
except Exception as e:
    print("\n" + "="*50)
    print("CRITICAL ERROR: Could not load pyRevit modules.")
    print("Technical error type: " + str(type(e)))
    print("Technical error: " + str(e))
    import traceback
    traceback.print_exc()
    print("="*50 + "\n")
    sys.exit(1)

# 2. ONLY NOW add the custom library path
import os
lib_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'lib'))
if lib_path not in sys.path:
    # Use append (not insert) to prioritize Revit's own libraries
    sys.path.append(lib_path)

# 3. Import our custom runner
try:
    from revit_mcp.runner import start_mcp_server
    print("Gemini MCP Runner loaded.")
except Exception as e:
    print("Error loading MCP Runner: " + str(e))
    # We use a lazy import for traceback to keep it light
    import traceback
    traceback.print_exc()

def main():
    output = script.get_output()
    output.print_md("# Gemini MCP Server")
    
    print("Attempting to start background service...")
    try:
        start_mcp_server()
        output.print_md("### Server Status: **ACTIVE** (Port 8001)")
    except Exception as e:
        print("Error starting server: " + str(e))

    forms.alert("Gemini MCP Server is now running in the background.\n\nKeep the Output window open for monitoring.\n\nClick OK only when you want to stop the script.", 
                title="Gemini MCP")

if __name__ == '__main__':
    main()
