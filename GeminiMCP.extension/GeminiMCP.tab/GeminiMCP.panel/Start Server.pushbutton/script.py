#! python3
# -*- coding: utf-8 -*-
"""
Gemini MCP Server - Start Button Script

Uses direct drain_queue() on the script thread + Application.DoEvents()
to pump Windows messages, preventing the STA thread deadlock that occurs
when Revit's view regeneration fires COM callbacks after Transaction.Commit().
"""
import sys
import os
import time

lib_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'lib'))
if lib_path not in sys.path:
    sys.path.append(lib_path)

from pyrevit import script

# Import message pump - critical for STA thread compatibility
try:
    import clr
    clr.AddReference('System.Windows.Forms')
    from System.Windows.Forms import Application as WinForms
    _has_doevents = True
except:
    _has_doevents = False

def pump():
    """Pump Windows messages to keep STA message queue clear."""
    if _has_doevents:
        try:
            WinForms.DoEvents()
        except:
            pass

def main():
    # Clear stale cached modules
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("revit_mcp"):
            del sys.modules[mod_name]

    output = script.get_output()
    output.close_others(all_open_outputs=True)

    try:
        from revit_mcp.runner import start_mcp_server
        success = start_mcp_server()
    except Exception as e:
        import traceback
        output.print_md("## ❌ Failed to Start")
        output.print_md("**Error:** `{}`\n```\n{}\n```".format(str(e), traceback.format_exc()))
        return

    if not success:
        output.print_md("## ⚠️ Server already running on Port 8001.")
        return

    output.print_md("## ✅ Gemini MCP Server: Active")
    output.print_md("- **Port:** 8001")
    output.print_md("- **Inspector:** `http://localhost:8001/sse`")
    output.print_md("- **DoEvents pump:** `{}`".format("✅ Active" if _has_doevents else "⚠️ Unavailable"))
    output.print_md("---")
    output.print_md("⚠️ **Minimize** this window — do NOT close it.")

    from revit_mcp.event_handler import drain_queue

    # KEEP-ALIVE + DISPATCH LOOP
    # pump() is called every iteration to prevent STA message queue starvation
    # which would cause Transaction.Commit() to deadlock on Revit view regeneration.
    try:
        while True:
            drain_queue()   # Execute any pending Revit API work
            pump()          # Pump Windows messages to unblock STA callbacks
            time.sleep(0.05)  # 50ms - short sleep, minimal message starvation
    except:
        pass

    output.print_md("---")
    output.print_md("🛑 Server stopped.")

if __name__ == '__main__':
    main()
