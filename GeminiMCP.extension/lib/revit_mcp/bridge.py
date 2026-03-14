# -*- coding: utf-8 -*-
import threading
import queue
import clr

# --- NO-INTERFACE IDLING DISPATCHER FOR REVIT 2026 ---
# Renamed to 'bridge.py' to avoid Revit's module cache.

_work_queue = queue.Queue()
_is_registered = False

def _idle_callback_v2(sender, args):
    """Event handler that runs when Revit is idle."""
    try:
        while not _work_queue.empty():
            func, args_list, holder = _work_queue.get_nowait()
            try:
                holder['result'] = func(*args_list)
            except Exception as e:
                holder['error'] = e
            finally:
                holder['done'].set()
    except Exception:
        pass

def init_bridge(uiapp):
    """Subscribes to the Idling event. Must call on Main Thread."""
    global _is_registered
    if not _is_registered:
        try:
            # UIApplication.Idling += EventHandler<IdlingEventArgs>
            uiapp.Idling += _idle_callback_v2
            _is_registered = True
            return True
        except Exception as e:
            # If for some reason Idling isn't accessible, we print to trace
            print("Gemini Bridge Error: " + str(e))
            return False
    return True

def run_on_main_thread(func, *args):
    """Submits work to Revit and waits for completion."""
    holder = {
        'result': None,
        'error': None,
        'done': threading.Event(),
    }
    _work_queue.put((func, args, holder))
    
    # Wait up to 30 seconds for Revit to pick it up
    completed = holder['done'].wait(timeout=30)
    if not completed:
        raise RuntimeError("Revit is busy or bridge failed (30s timeout)")
    
    if holder['error']:
        raise holder['error']
    return holder['result']

class BridgeProxy(object):
    def run_on_main_thread(self, *args, **kwargs):
        # Handle cases where it might be called with params or multiple args
        if len(args) > 1:
            return run_on_main_thread(args[0], *args[1:])
        return run_on_main_thread(args[0])

# Global instance for easy importing
mcp_event_handler = BridgeProxy()
