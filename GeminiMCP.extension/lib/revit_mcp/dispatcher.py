# -*- coding: utf-8 -*-
# NOTE: Do NOT import Autodesk.Revit.DB at module level.
# This module is loaded on the background Uvicorn thread. All DB access
# must stay INSIDE closures that run via mcp_event_handler (Revit main thread).
import json
from .gemini_client import client
from .agent_prompts import *
from .revit_workers import RevitWorkers, execute_in_transaction_group
from .bridge import mcp_event_handler

class Orchestrator:
    def __init__(self):
        self.workers = None

    def _init_workers(self, uiapp):
        doc = uiapp.ActiveUIDocument.Document
        self.workers = RevitWorkers(doc)

    def run_full_stack(self, uiapp, user_prompt):
        self._init_workers(uiapp)
        doc = uiapp.ActiveUIDocument.Document
        # RUN BRAIN ON BACKGROUND THREAD
        return self._orchestrate(doc, user_prompt)

    def _orchestrate(self, doc, user_prompt):
        # Reload only pure-Python modules (NOT building_generator - it has clr/DB imports)
        import importlib
        from . import revit_workers, agent_prompts
        importlib.reload(revit_workers)
        importlib.reload(agent_prompts)
        from .revit_workers import RevitWorkers

        
        def gather_state():
            import Autodesk.Revit.DB as DB # type: ignore
            levels = []
            for l in DB.FilteredElementCollector(doc).OfClass(DB.Level):
                name = l.Name
                if name.startswith("AI Level") or name.startswith("AI_Level"):
                    levels.append(l)
            levels.sort(key=lambda x: x.Elevation)
            count = len(levels)
            fh = 4000
            if count >= 2:
                from Autodesk.Revit.DB import UnitUtils, UnitTypeId # type: ignore
                fh = UnitUtils.ConvertFromInternalUnits(levels[1].Elevation - levels[0].Elevation, UnitTypeId.Millimeters)
            return count, fh

        try:
            cur_levels, cur_fh = mcp_event_handler.run_on_main_thread(gather_state)
            storeys = max(1, cur_levels - 1)
            state_text = f"CURRENT BIM STATE: {storeys} storeys, {cur_fh:.1f}mm floor height. If user says 'maintain levels', output exactly {storeys} for levels and {cur_fh:.1f} for level_height."
        except Exception as e:
            print(f"Dispatcher: Error gathering state: {e}")
            state_text = ""

        # 1. Generate Master Manifest (Fast-Track)
        print(f"Dispatcher: Injected State Text -> {state_text}")
        print("Dispatcher: Generating Fast-Track Manifest...")
        manifest_json = client.generate_content(DISPATCHER_PROMPT + "\n" + state_text + "\nUser Request: " + user_prompt)
        print(f"Dispatcher: Gemini Payload -> {manifest_json}")
        
        try:
            manifest = json.loads(self._extract_json(manifest_json))
        except:
            return "Error: Failed to parse Manifest."

        # 2. Single Transaction Batching
        def main_action():
            import Autodesk.Revit.DB as DB # type: ignore
            # Init workers on the main thread where DB access is safe
            workers = RevitWorkers(doc)
            print("Fast-Track: Executing BIM Manifest...")
            t = DB.Transaction(doc, "Fast BIM Build")
            t.Start()
            try:
                results = workers.execute_fast_manifest(manifest)
                t.Commit()
            except Exception as e:
                t.RollBack()
                raise e
            
            # BIM Health & Documentation
            workers.perform_global_cleanup()
            workers.generate_submission_set()
            
            return "Fast-Track Build Completed in record time."

        return mcp_event_handler.run_on_main_thread(main_action)

    def _extract_json(self, text):
        if "```json" in text:
            return text.split("```json")[1].split("```")[0].strip()
        return text.strip()

orchestrator = Orchestrator()
