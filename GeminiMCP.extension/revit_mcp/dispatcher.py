# -*- coding: utf-8 -*-
# NOTE: Do NOT import Autodesk.Revit.DB at module level.
# This module is loaded on the background Uvicorn thread. All DB access
# must stay INSIDE closures that run via mcp_event_handler (Revit main thread).
import json
from revit_mcp.gemini_client import client
from revit_mcp.agent_prompts import *
from revit_mcp.revit_workers import RevitWorkers, execute_in_transaction_group
from revit_mcp.bridge import mcp_event_handler
from revit_mcp.building_generator import BuildingSystem

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
            from revit_mcp.building_generator import get_model_registry # type: ignore
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
                    height_str += f"L{i+1}:{h_mm:.0f} "
                    
                    # Also detect footprint overrides from walls
                    w_tag, l_tag = f"AI_Wall_L{i+1}_S", f"AI_Wall_L{i+1}_W"
                    if w_tag in registry and l_tag in registry:
                        w_wall, l_wall = doc.GetElement(registry[w_tag]), doc.GetElement(registry[l_tag])
                        if w_wall and l_wall and hasattr(w_wall.Location, "Curve") and hasattr(l_wall.Location, "Curve"):
                            w_mm = UnitUtils.ConvertFromInternalUnits(w_wall.Location.Curve.Length, UnitTypeId.Millimeters)
                            l_mm = UnitUtils.ConvertFromInternalUnits(l_wall.Location.Curve.Length, UnitTypeId.Millimeters)
                            overrides_str += f"L{i+1}:{w_mm:.0f}x{l_mm:.0f} "
            
            # Comprehensive Stats per Level
            per_floor_stats = {}
            for lvl in levels:
                l_id = lvl.Id
                level_filter = DB.ElementLevelFilter(l_id)
                
                def count_cat(category_bit):
                    return DB.FilteredElementCollector(doc).OfCategory(category_bit).WhereElementIsNotElementType().WherePasses(level_filter).GetElementCount()
                
                per_floor_stats[lvl.Name] = {
                    "walls": DB.FilteredElementCollector(doc).OfClass(DB.Wall).WherePasses(level_filter).GetElementCount(),
                    "floors": DB.FilteredElementCollector(doc).OfClass(DB.Floor).WherePasses(level_filter).GetElementCount(),
                    "doors": count_cat(DB.BuiltInCategory.OST_Doors),
                    "windows": count_cat(DB.BuiltInCategory.OST_Windows),
                    "columns": count_cat(DB.BuiltInCategory.OST_Columns) + count_cat(DB.BuiltInCategory.OST_StructuralColumns),
                }

            # Global Totals
            wall_count = DB.FilteredElementCollector(doc).OfClass(DB.Wall).GetElementCount()
            floor_count = DB.FilteredElementCollector(doc).OfClass(DB.Floor).GetElementCount()
            door_count = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Doors).WhereElementIsNotElementType().GetElementCount()
            win_count = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Windows).WhereElementIsNotElementType().GetElementCount()
            col_count = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Columns).WhereElementIsNotElementType().GetElementCount()
            scol_count = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_StructuralColumns).WhereElementIsNotElementType().GetElementCount()
            
            # Measure Column Span (preserving existing fix)
            col_span_mm = 10000 # Default fallback (10m)
            ai_cols = []
            col_collector = DB.FilteredElementCollector(doc).WhereElementIsNotElementType()
            from System.Collections.Generic import List
            cat_list = List[DB.BuiltInCategory]()
            cat_list.Add(DB.BuiltInCategory.OST_Columns)
            cat_list.Add(DB.BuiltInCategory.OST_StructuralColumns)
            col_collector.WherePasses(DB.ElementMulticategoryFilter(cat_list))
            
            for el in col_collector:
                p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if not p: p = el.LookupParameter("Comments")
                if p and p.HasValue and p.AsString() and "AI_Col_L1_" in p.AsString():
                    ai_cols.append(el)
            
            if len(ai_cols) >= 2:
                pts = [c.Location.Point for c in ai_cols]
                pts.sort(key=lambda p: (round(p.X, 2), round(p.Y, 2)))
                for i in range(len(pts)-1):
                    p1, p2 = pts[i], pts[i+1]
                    dist_ft = p1.DistanceTo(p2)
                    if (abs(p1.X - p2.X) < 0.1 or abs(p1.Y - p2.Y) < 0.1) and dist_ft > 1.0:
                        col_span_mm = round(dist_ft * 304.8 / 100.0) * 100.0
                        break

            stats = {
                "levels": count,
                "total_stats": {
                    "walls": wall_count,
                    "floors": floor_count,
                    "doors": door_count,
                    "windows": win_count,
                    "columns": col_count + scol_count
                },
                "per_floor_breakdown": per_floor_stats,
                "current_column_span": col_span_mm
            }
            
            return count, height_str, overrides_str, stats

        # OPTIMIZATION: Advanced Cache with 30s TTL
        import time
        now = time.time()
        refresh_needed = True
        
        if hasattr(self, "_cached_state") and hasattr(self, "_cache_time"):
            age = now - self._cache_time
            # If the state is fresh (<30s), we can reuse it even for minor edits 
            # to speed up the interaction, unless it's a major "create" or "delete".
            force_refresh = any(x in user_prompt.lower() for x in ["create", "delete", "clear", "wipe"])
            if age < 30.0 and not force_refresh:
                refresh_needed = False
        
        if not refresh_needed:
            self.log("[{}] Dispatcher: Using cached BIM state (Age: {:.1f}s)".format(time.time(), now - self._cache_time))
            state_text = self._cached_state
        else:
            try:
                self.log("[{}] Dispatcher: Gathering fresh BIM state from Revit...".format(time.time()))
                cur_levels, cur_heights, cur_overrides, cur_stats = mcp_event_handler.run_on_main_thread(gather_state)
                self.log("BIM state gathered: {} levels found.".format(cur_levels))
                storeys = max(1, cur_levels - 1)
                state_text = f"CURRENT BIM STATE: {storeys} storeys. "
                if cur_heights: state_text += f"\nEXISTING HEIGHTS: {cur_heights}"
                if cur_overrides: state_text += f"\nEXISTING OVERRIDES: {cur_overrides}"
                if cur_stats: 
                    state_text += f"\nPROJECT TOTALS: {json.dumps(cur_stats['total_stats'])}"
                    state_text += f"\nPER-FLOOR BREAKDOWN: {json.dumps(cur_stats['per_floor_breakdown'])}"
                    state_text += f"\nDETECTED COLUMN SPAN: {cur_stats['current_column_span']}mm"
                state_text += f"\nCRITICAL: Refer to PER-FLOOR BREAKDOWN for detailed queries. Preserve existing state unless asked to change."
                self._cached_state = state_text
                self._cache_time = now
            except Exception as e:
                self.log(f"Error gathering state: {e}")
                state_text = ""

        # 1. Generate Master Manifest (Fast-Track)
        self.log("Step 2: Requesting building plan from Gemini AI (model: {})".format(client.model))
        ai_start = time.time()
        manifest_json = client.generate_content(DISPATCHER_PROMPT + "\n" + state_text + "\nUser Request: " + user_prompt)
        ai_duration = time.time() - ai_start
        self.log("Step 3: Manifest received from AI. (Time: {:.2f}s). Parsing...".format(ai_duration))
        self.log(f"Gemini Payload -> {manifest_json}")
        
        try:
            manifest_str = self._extract_json(manifest_json)
            manifest = json.loads(manifest_str)
            self.log("Manifest parsed successfully.")
            
            # UNWRAP AI TOOL-CALL-STYLE WRAPPERS
            for wrapper in ["orchestrate_build", "edit_entire_building_dimensions"]:
                if wrapper in manifest and len(manifest) == 1:
                    self.log(f"Dispatcher: Unwrapping manifest from '{wrapper}' key.")
                    manifest = manifest[wrapper]
                    break
            
            # CHECK FOR QUERY RESPONSE
            if "response" in manifest and not any(k in manifest for k in ["project_setup", "levels", "shell"]):
                self.log("Dispatcher: AI detected a QUESTION. Returning natural language response.")
                return str(manifest["response"])
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
