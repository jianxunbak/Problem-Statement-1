# -*- coding: utf-8 -*-
# NOTE: Do NOT import Autodesk.Revit.DB at module level.
# This module is loaded on the background Uvicorn thread. All DB access
# must stay INSIDE closures that run via mcp_event_handler (Revit main thread).
import json
from .gemini_client import client
from .agent_prompts import *
from .revit_workers import RevitWorkers, execute_in_transaction_group
from .bridge import mcp_event_handler
from .building_generator import BuildingSystem

class Orchestrator:
    def __init__(self):
        self.workers = None
        self.generator = None

    def run_full_stack(self, uiapp, user_prompt):
        # Do NOT access uiapp.ActiveUIDocument here (thread violation).
        # Simply pass uiapp or rely on the server's global _uiapp if preferred,
        # but here we pass it down.
        return self._orchestrate(uiapp, user_prompt)

    def log(self, message):
        client.log(message)

    def _orchestrate(self, uiapp, user_prompt):
        # All imports moved to top of module
        self.log("Dispatcher: Gathering current BIM state...")
        def gather_state():
            import Autodesk.Revit.DB as DB # type: ignore
            doc = uiapp.ActiveUIDocument.Document
            from .building_generator import get_model_registry # type: ignore
            registry = get_model_registry(doc)
            
            levels = []
            for l in DB.FilteredElementCollector(doc).OfClass(DB.Level):
                name = l.Name
                if name.startswith("AI Level") or name.startswith("AI_Level"):
                    levels.append(l)
            levels.sort(key=lambda x: x.Elevation)
            count = len(levels)
            
            from Autodesk.Revit.DB import UnitUtils, UnitTypeId # type: ignore
            height_str = ""
            overrides_str = ""
            
            if count >= 2:
                for i in range(count - 1):
                    diff_ft = levels[i+1].Elevation - levels[i].Elevation
                    h_mm = UnitUtils.ConvertFromInternalUnits(diff_ft, UnitTypeId.Millimeters)
                    height_str += f"Floor {i+1}: {h_mm:.1f}mm. "
                    
                    # Also detect footprint overrides from walls
                    w_tag = f"AI_Wall_L{i+1}_S"
                    l_tag = f"AI_Wall_L{i+1}_W"
                    if w_tag in registry and l_tag in registry:
                        w_wall = doc.GetElement(registry[w_tag])
                        l_wall = doc.GetElement(registry[l_tag])
                        if w_wall and l_wall and hasattr(w_wall.Location, "Curve") and hasattr(l_wall.Location, "Curve"):
                            w_mm = UnitUtils.ConvertFromInternalUnits(w_wall.Location.Curve.Length, UnitTypeId.Millimeters)
                            l_mm = UnitUtils.ConvertFromInternalUnits(l_wall.Location.Curve.Length, UnitTypeId.Millimeters)
                            overrides_str += f"Floor {i+1} footprint: {w_mm:.0f}x{l_mm:.0f}mm. "
            
            return count, height_str, overrides_str

        try:
            cur_levels, cur_heights, cur_overrides = mcp_event_handler.run_on_main_thread(gather_state)
            self.log("BIM state gathered: {} levels found.".format(cur_levels))
            storeys = max(1, cur_levels - 1)
            state_text = f"CURRENT BIM STATE: {storeys} storeys. "
            if cur_heights: state_text += f"\nEXISTING HEIGHTS: {cur_heights}"
            if cur_overrides: state_text += f"\nEXISTING OVERRIDES: {cur_overrides}"
            state_text += f"\nCRITICAL: If user edits the building, you MUST preserve these individual floor heights and overrides in your JSON output unless the user specifically asks to change them! Keep them as 'height_overrides' and 'floor_overrides'."
        except Exception as e:
            self.log(f"Error gathering state: {e}")
            state_text = ""

        # 1. Generate Master Manifest (Fast-Track)
        # self.log(f"Injected State Text -> {state_text}")
        self.log("Step 2: Requesting building plan from Gemini AI...")
        manifest_json = client.generate_content(DISPATCHER_PROMPT + "\n" + state_text + "\nUser Request: " + user_prompt)
        self.log("Step 3: Manifest received from AI. Parsing...")
        self.log(f"Gemini Payload -> {manifest_json}")
        
        try:
            manifest_str = self._extract_json(manifest_json)
            manifest = json.loads(manifest_str)
            self.log("Manifest parsed successfully.")
        except Exception as e:
            import traceback
            err = "Manifest Parsing Error: {}\n{}".format(str(e), traceback.format_exc())
            self.log(err)
            return err

        # 2. Single Transaction Batching
        def main_action():
            import Autodesk.Revit.DB as DB # type: ignore
            from .building_generator import BuildingSystem
            doc = uiapp.ActiveUIDocument.Document
            
            # Use the high-performance RevitWorkers
            self.log("Step 4: Initializing RevitWorkers and Executing Manifest (Main Thread)...")
            from .revit_workers import RevitWorkers
            workers = RevitWorkers(doc)
            
            try:
                results = workers.execute_fast_manifest(manifest)
                self.log("Manifest execution summary: {}".format(results.get('summary', 'Success')))
                
                # Global cleanup for premium geometry joins
                workers.perform_global_cleanup()
                workers.generate_submission_set()
                
                return "Build Completed successfully via RevitWorkers."
            except Exception as e:
                import traceback
                error_trace = traceback.format_exc()
                self.log(f"Manifest execution FAILED: {e}\n{error_trace}")
                raise e
            
        self.log("Step 6: Submitting build action to Revit main thread...")
        return mcp_event_handler.run_on_main_thread(main_action)

    def _extract_json(self, text):
        if "```json" in text:
            return text.split("```json")[1].split("```")[0].strip()
        
        # Robustness: Remove common hallucinated wrappers
        data = text.strip()
        if data.startswith("orchestrate_build(") and data.endswith(")"):
            data = data[len("orchestrate_build("):-1].strip()
        if data.startswith("edit_entire_building_dimensions(") and data.endswith(")"):
            data = data[len("edit_entire_building_dimensions("):-1].strip()
            
        # Final Fallback: Find the first { and last }
        try:
            start = data.find("{")
            end = data.rfind("}")
            if start != -1 and end != -1:
                return data[start:end+1].strip()
        except: pass
            
        return data.strip()

orchestrator = Orchestrator()
