import time
import json
import asyncio
import threading
from collections import deque

class BuildProgressTracker:
    def __init__(self, ctx=None, loop=None, callback=None):
        self.ctx = ctx
        self.loop = loop
        self._callback = callback
        self.start_time = None
        self.end_time = None
        self.elements_planned = {
            "floors": 0,
            "walls": 0,
            "columns": 0,
            "lifts": 0,
            "staircases": 0
        }
        self.elements_created = {
            "levels": 0,
            "walls": 0,
            "floors": 0,
            "columns": 0,
            "lifts": 0,
            "staircases": 0,
            "stair_runs": 0,
        }
        self.adjustments = []
        self.narrative_history = []
        # Thread-safe queue for messages
        self._msg_queue = deque()
        self._stop_event = threading.Event()
        self._poller_task = None

    def start(self):
        self.start_time = time.time()
        from revit_mcp.runner import log
        log("DEBUG: ProgressTracker.start() - Initializing poller.")
        # Start the background poller on the uvicorn loop
        if self.loop and self.ctx:
            self.loop.call_soon_threadsafe(self._start_poller)
        else:
            log(f"DEBUG: ProgressTracker.start() - FAILED: loop={bool(self.loop)}, ctx={bool(self.ctx)}. Streaming disabled.")

        self.report("Sending prompt to AI... Waiting for building manifest.")

    def _start_poller(self):
        """Starts the async polling task on the correct loop."""
        from revit_mcp.runner import log
        try:
            if self.loop:
                self._poller_task = self.loop.create_task(self._poll_messages())
                log("DEBUG: ProgressTracker Poller Task CREATED on loop.")
        except Exception as e:
            log(f"DEBUG: ProgressTracker Poller Task FAILED to create: {e}")

    async def _poll_messages(self):
        """Async task that drains the queue and sends updates to the client."""
        from revit_mcp.runner import log
        log("DEBUG: ProgressTracker Poller LOOP ENTERED.")
        try:
            # Continue polling as long as not stopped OR queue is not empty
            while not self._stop_event.is_set() or self._msg_queue:
                while self._msg_queue:
                    msg = self._msg_queue.popleft()
                    if self.ctx:
                        # Log sending for debug
                        log(f"DEBUG: ProgressTracker Sending -> {msg}")
                        try:
                            # Attempt 1: Standard FastMCP info
                            await self.ctx.info(msg)
                            
                            # Attempt 2: Redundant raw notification (for broader client support)
                            try:
                                if hasattr(self.ctx, "session") and self.ctx.session:
                                    await self.ctx.session.send_notification(
                                        "notifications/message",
                                        {"level": "info", "description": msg}
                                    )
                                    log("DEBUG: ProgressTracker Raw Notification SENT.")
                            except Exception as e2:
                                log(f"DEBUG: ProgressTracker Raw Notify FAILED: {e2}")
                        except Exception as e:
                            log(f"DEBUG: ProgressTracker ctx.info FAILED: {e}")
                    else:
                        log("DEBUG: ProgressTracker SKIPPED send - No Context.")
                
                # Check for stop signal after draining
                if self._stop_event.is_set() and not self._msg_queue:
                    break
                    
                await asyncio.sleep(0.05) # Faster polling (20Hz)
            log("DEBUG: ProgressTracker Poller LOOP EXITING (Drained).")
        except Exception as e:
            log(f"DEBUG: ProgressTracker Poller CRASHED: {e}")
                
    def stop(self):
        """Signal the poller to stop."""
        from revit_mcp.runner import log
        log("DEBUG: ProgressTracker.stop() signaled.")
        self._stop_event.set()
        self.end_time = time.time()
        
    def analyze_manifest(self, manifest_json):
        if not manifest_json:
            return

        try:
            manifest = json.loads(manifest_json) if isinstance(manifest_json, str) else manifest_json

            # unwrap
            for wrapper in ["orchestrate_build", "edit_entire_building_dimensions"]:
                if wrapper in manifest and len(manifest) == 1:
                    manifest = manifest[wrapper]
                    break

            setup = manifest.get("project_setup", {})
            shell = manifest.get("shell", {})
            levels = int(setup.get("levels", setup.get("storeys", 1)))
            level_h = setup.get("level_height", 4000)
            width = shell.get("width", 0)
            length = shell.get("length", 0)
            col_spacing = shell.get("column_spacing", 10000)
            stair_count = int(manifest.get("staircases", {}).get("count", 2))

            self.elements_planned["floors"] = levels
            if "staircases" in manifest:
                self.elements_planned["staircases"] = stair_count

            # Dynamic time estimate based on actual building complexity
            # Levels: ~0.3s each (create + constraints)
            t_levels = levels * 0.3
            # Shell walls: 4 per level × ~0.2s
            t_walls = levels * 4 * 0.2
            # Floors: 1 per level × ~0.3s (CurveLoop + voids)
            t_floors = levels * 0.3
            # Columns: estimate grid count from footprint and spacing
            if width > 0 and length > 0 and col_spacing > 0:
                nx = max(2, int(width / col_spacing) + 1)
                ny = max(2, int(length / col_spacing) + 1)
                total_cols = nx * ny * levels
                t_cols = total_cols * 0.02
            else:
                total_cols = 0
                t_cols = levels * 2.0
            # Lifts: ~1s per level for wall generation
            t_lifts = levels * 1.0
            # Staircases: ~1.5s per staircase per level (walls + floors + runs)
            t_stairs = stair_count * levels * 1.5
            # Overhead (nuclear lockdown, disjoint, cleanup)
            t_overhead = 3.0

            est_total = t_levels + t_walls + t_floors + t_cols + t_lifts + t_stairs + t_overhead
            est_min = max(8, int(est_total * 0.7))
            est_max = int(est_total * 1.4)

            # Build a descriptive summary
            parts = [f"{levels} levels"]
            if width > 0 and length > 0:
                parts.append(f"{width/1000:.0f}m x {length/1000:.0f}m footprint")
            if level_h:
                parts.append(f"{level_h}mm typical floor height")
            if col_spacing > 0:
                parts.append(f"{col_spacing/1000:.0f}m column grid")
            parts.append(f"{stair_count} staircases")

            self.report(
                f"Manifest received: {', '.join(parts)}. "
                f"Estimated build time: {est_min}-{est_max}s."
            )

        except Exception:
            self.report("Manifest received. Building...")

    def report(self, msg, is_narrative=False):
        """Queue a status update for the background poller, or deliver via callback."""
        from revit_mcp.runner import log
        
        # Add micro-timestamp for uniqueness to prevent client squashing
        t_now = time.strftime("%H:%M:%S")
        unique_msg = f"[{t_now}] {msg}"
        
        log(f"PROGRESS: {unique_msg}")

        if is_narrative:
            self.narrative_history.append(msg)

        if self._callback:
            try:
                self._callback(msg)
            except Exception as e:
                log(f"PROGRESS CALLBACK ERROR: {e}")
            return

        # Simple thread-safe append (deque.append is atomic in CPython)
        self._msg_queue.append(unique_msg)

    def record_created(self, category, count=1):
        """Track actual elements created during the build."""
        if category in self.elements_created:
            self.elements_created[category] += count

    def log_adjustment(self, adjustment_msg):
        """Track explicit design adjustments (e.g. floor height changed to fit stairs)."""
        if adjustment_msg not in self.adjustments:
            self.adjustments.append(adjustment_msg)
            self.report(f"Adjustment: {adjustment_msg}")

    def generate_final_report(self, base_summary="Build Completed successfully."):
        self.stop()
        duration = self.end_time - self.start_time if self.start_time else 0

        report = []
        report.append(f"Status: {base_summary}")
        report.append(f"Duration: {duration:.1f} seconds")

        if self.narrative_history:
            report.append("\nArchitectural Logic:")
            for item in self.narrative_history:
                report.append(item)
        report.append("\nConstructed Elements:")
        for key, val in self.elements_created.items():
            if val > 0:
                label = key.replace("_", " ").capitalize()
                report.append(f"- {label}: {val}")

        # Adjustments
        if self.adjustments:
            report.append("\nDesign Adjustments:")
            for adj in self.adjustments:
                report.append(f"- {adj}")

        return "\n".join(report)

