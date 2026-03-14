# -*- coding: utf-8 -*-
import threading
import queue
import clr
import time

# --- REFACTORED FOR REVIT 2026: USING IDLING EVENT (NO INTERFACE HEADACHE) ---
# This version avoids the 'IExternalEventHandler' interface crash entirely
# by using the UIApplication.Idling event, which is a standard .NET event.

# Global work queue
_work_queue = queue.Queue()
_idling_registered = False

def idling_handler(sender, args):
    """
    Standard Revit Idling Event Handler.
    Executed by Revit when the UI is idle.
    """
    try:
        # Process ONLY ONE item per idle tick or all? All is usually fine.
        while not _work_queue.empty():
            try:
                func, args_list, result_holder = _work_queue.get_nowait()
                result_holder['result'] = func(*args_list)
            except Exception as e:
                result_holder['error'] = e
            finally:
                result_holder['done'].set()
    except:
        pass

def init_dispatcher(uiapp):
    """
    Registers the Idling event. 
    Must be called from the main thread (script.py).
    """
    global _idling_registered
    if not _idling_registered:
        from Autodesk.Revit.UI.Events import IdlingEventArgs # type: ignore
        # Standard .NET event subscription syntax in Python.NET
        uiapp.Idling += idling_handler
        _idling_registered = True
    return True

def run_on_main_thread(func, *args, **kwargs):
    """
    Submit work to the queue. 
    It will be executed automatically the next time Revit is idle.
    """
    result_holder = {
        'result': None,
        'error': None,
        'done': threading.Event(),
    }
    _work_queue.put((func, args, result_holder))
    
    # We don't need to 'Raise' anything; Idling fires naturally.
    # However, we wait for the result.
    completed = result_holder['done'].wait(timeout=30)
    if not completed:
        raise RuntimeError("Timeout: Revit was too busy to process the request (30s).")
    
    if result_holder['error'] is not None:
        raise result_holder['error']
    return result_holder['result']

class DispatcherProxy(object):
    def run_on_main_thread(self, *args, **kwargs):
        return run_on_main_thread(*args, **kwargs)

mcp_event_handler = DispatcherProxy()
