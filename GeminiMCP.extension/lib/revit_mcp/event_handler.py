# -*- coding: utf-8 -*-
import threading

# We avoid any Revit/CLR imports at top level to prevent initialization conflicts.

class McpEventHandler(object):
    """
    Utility to run code on the Revit UI thread using pyRevit's built-in system.
    This avoids the 'interface takes exactly one argument' error with custom handlers.
    """
    def __init__(self):
        self._result_event = threading.Event()
        self._current_result = None
        self._current_error = None

    def run_on_main_thread(self, func, *args, **kwargs):
        """
        Uses pyRevit's execute_in_revit_context to marshal the call.
        """
        # We import pyrevit inside the function to ensure it is fully initialized
        from pyrevit.revit import events
        
        self._result_event.clear()
        self._current_result = None
        self._current_error = None

        def wrapper():
            try:
                self._current_result = func(*args, **kwargs)
            except Exception as e:
                self._current_error = e
            finally:
                self._result_event.set()

        # pyRevit's built-in thread safe executor
        events.execute_in_revit_context(wrapper)

        # Wait for the result
        self._result_event.wait(timeout=10) # 10s safety timeout
        
        if self._current_error:
            raise self._current_error
            
        return self._current_result

# Singleton instance
mcp_event_handler = McpEventHandler()
