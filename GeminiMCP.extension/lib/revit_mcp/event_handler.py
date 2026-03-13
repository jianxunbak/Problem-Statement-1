# -*- coding: utf-8 -*-
import clr
clr.AddReference('RevitAPI')
clr.AddReference('RevitAPIUI')

import Autodesk.Revit.DB as DB
from Autodesk.Revit.UI import IExternalEventHandler, ExternalEvent
import threading

try:
    import queue
except ImportError:
    import Queue as queue

class McpEventHandler(IExternalEventHandler):
    """
    Event handler to execute actions on the main Revit UI thread.
    Required for any Transactions or deep document modifications.
    """
    def __init__(self):
        super(McpEventHandler, self).__init__()
        self._action_queue = queue.Queue()
        self._result_event = threading.Event()
        self._current_result = None
        self._current_error = None

    def Execute(self, app):
        """Execute the next action in the queue on the UI Thread."""
        try:
            # Get the action and args from the queue
            action_func, args, kwargs = self._action_queue.get_nowait()
            
            # Execute it
            self._current_result = action_func(*args, **kwargs)
            self._current_error = None
        except queue.Empty:
            pass
        except Exception as e:
            self._current_error = e
            self._current_result = None
        finally:
            # Signal completion
            self._result_event.set()

    def GetName(self):
        return "Gemini FastMCP PyRevit Handler"

    def run_on_main_thread(self, func, *args, **kwargs):
        """
        Called from the background server thread.
        Queues a function to be executed and waits for the result synchronously.
        """
        self._result_event.clear()
        self._current_result = None
        self._current_error = None
        
        # Enqueue the function and its arguments
        self._action_queue.put((func, args, kwargs))
        
        # Generate and raise the external event (lazy init)
        if not hasattr(self, '_external_event'):
            self._external_event = ExternalEvent.Create(self)
        
        self._external_event.Raise()
        
        # Wait for the UI thread to finish the action
        self._result_event.wait()
        
        if self._current_error:
            raise self._current_error
            
        return self._current_result

# Create a singleton instance
mcp_event_handler = McpEventHandler()
