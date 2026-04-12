import threading
import queue
import datetime
import os

# --- Bridge State ---
from .utils import get_log_path
LOG_PATH = get_log_path()
_work_queue = queue.Queue()
_uiapp = None

def pump_commands(uiapp):
    """
    Drains the bridge work queue. 
    This MUST be called from the Revit Main Thread (e.g. via DispatcherTimer).
    """
    global _uiapp
    _uiapp = uiapp
    
    while not _work_queue.empty():
        try:
            # Non-blocking get
            func, args, kwargs, event, result_wrapper, queued_at = _work_queue.get_nowait()
            
            latency = (datetime.datetime.now() - queued_at).total_seconds()
            if latency > 1.0:
                with open(LOG_PATH, "a") as f:
                    f.write("[{}] Bridge: HIGH LATENCY ({:.2f}s) for {}\n".format(
                        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), latency, str(func)))
            
            try:
                # Execute the Revit-dependent function on the main thread
                result = func(*args, **kwargs)
                result_wrapper['data'] = result
            except Exception as e:
                import traceback
                # We can't use 'from .runner import log' easily here due to potential circularity
                # direct write to log file
                with open(LOG_PATH, "a") as f:
                    f.write("[{}] Bridge EXECUTION ERROR: {}\n{}\n".format(
                        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        str(e), traceback.format_exc()))
                result_wrapper['error'] = e
            finally:
                event.set() # Release the caller thread
                _work_queue.task_done()
        except queue.Empty:
            break
        except Exception as ex:
            break

def run_on_main_thread(func, *args, **kwargs):
    """
    Submit a function to be executed by the Bridge Pump on the Revit Main Thread.
    Blocks the caller thread until execution is complete.
    """
    event = threading.Event()
    result_wrapper = {'data': None, 'error': None}
    queued_at = datetime.datetime.now()
    
    _work_queue.put((func, args, kwargs, event, result_wrapper, queued_at))
    
    # Wait for completion (default 1200s to avoid infinite hang on massive generations)
    if not event.wait(1200):
        # Log timeout
        with open(LOG_PATH, "a") as f:
            f.write("[{}] Bridge: TIMEOUT waiting for main thread execution of {}\n".format(
                datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), str(func)))
        raise TimeoutError("Revit main thread did not respond within 1200s.")
        
    if result_wrapper['error']:
        raise result_wrapper['error']
        
    return result_wrapper['data']

def init_bridge(uiapp):
    """Bridge initialization. Now simpler since it's timer-based."""
    global _uiapp
    _uiapp = uiapp
    with open(LOG_PATH, "a") as f:
        f.write("[{}] Bridge: Timer-based bridge initialized (v8-STABLE).\n".format(
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    return True

def idling_handler(sender, args):
    """Event handler for UIApplication.Idling. Provides valid API context."""
    try:
        # sender is the UIApplication object
        pump_commands(sender)
    except:
        pass

# Mock class for backward compatibility
class MCPEventHandler:
    def run_on_main_thread(self, func, *args, **kwargs):
        return run_on_main_thread(func, *args, **kwargs)

mcp_event_handler = MCPEventHandler()
