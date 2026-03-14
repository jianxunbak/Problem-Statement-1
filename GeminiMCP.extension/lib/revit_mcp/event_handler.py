# -*- coding: utf-8 -*-
"""
Event handler: shared queue for dispatching Revit API calls.
The script thread's loop calls execute_in_revit_context(drain_queue)
which runs drain_queue in pyRevit's proper Revit event context.
No IExternalEventHandler / CLR interface needed.
"""
import threading
import queue

# Shared work queue
_work_queue = queue.Queue()


def drain_queue():
    """
    Process all pending work items.
    Must be called within a valid Revit execution context.
    """
    while not _work_queue.empty():
        try:
            func, args, result_holder = _work_queue.get_nowait()
        except queue.Empty:
            break
        try:
            result_holder['result'] = func(*args)
        except Exception as e:
            result_holder['error'] = e
        finally:
            result_holder['done'].set()


class McpEventHandler(object):
    """Submits work to the queue. Dispatch is done by the script thread."""

    def run_on_main_thread(self, func, *args, **kwargs):
        """Put work in queue and wait up to 15s for the script thread to process it."""
        result_holder = {
            'result': None,
            'error': None,
            'done': threading.Event(),
        }
        _work_queue.put((func, args, result_holder))

        completed = result_holder['done'].wait(timeout=15)
        if not completed:
            raise RuntimeError(
                "Timeout: Revit did not process the request within 15 seconds."
            )
        if result_holder['error'] is not None:
            raise result_holder['error']
        return result_holder['result']


# Singleton
mcp_event_handler = McpEventHandler()
