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
from revit_mcp.utils import load_presets, load_compliance

class Orchestrator:
    def __init__(self):
        self.workers = None
        self.generator = None

    def run_full_stack(self, uiapp, user_prompt, tracker=None):
        # Do NOT access uiapp.ActiveUIDocument here (thread violation).
        # Simply pass uiapp or rely on the server's global _uiapp if preferred,
        # but here we pass it down.
        return self._orchestrate(uiapp, user_prompt, tracker)

    def log(self, message):
        client.log(message)

    def _orchestrate(self, uiapp, user_prompt, tracker=None):
        # All imports moved to top of module
        self.log("Dispatcher: Gathering current BIM state...")
        if tracker: tracker.start()

        # Load Building Presets
        presets = load_presets()
        presets_text = ""
        if presets:
            presets_text = "\nBUILDING PRESETS (DNA):\n" + json.dumps(presets, indent=2)

        # Load Authority Compliance Rules and inject into prompt
        _c_lift   = load_compliance("lift_engineering")
        _c_fire   = load_compliance("fire_safety")
        _c_struct = load_compliance("structural")
        compliance_text = ""

        # --- START RAG INTEGRATION ---
        # Dynamically retrieve SCDF fire codes via Vertex AI RAG when enabled.
        # Falls back silently to static _c_fire if RAG is disabled or fails.
        import time as _time
        rag_rules = None
        try:
            from revit_mcp.config import RAG_ENABLED
            self.log(f"[RAG] RAG_ENABLED={RAG_ENABLED}")
            if RAG_ENABLED:
                from revit_mcp.agents.main_agent import extract_intent
                from revit_mcp.agents.sub_agent import run_retrieve_rules
                self.log("[RAG] Extracting building intent (regex)...")
                if tracker: tracker.report("🔎 Extracting building intent for authority code lookup...")
                intent = extract_intent(user_prompt)
                self.log(f"[RAG] Intent extracted: {intent}")
                if tracker: tracker.report(f"📐 Building intent: {intent.get('building_type', '?')}, {intent.get('storeys', '?')} storeys, topics: {intent.get('topics', [])}")
                _t_rag = _time.time()
                self.log("[RAG] Calling run_retrieve_rules...")
                rag_rules = run_retrieve_rules(intent, report=tracker.report if tracker else None)
                self.log(f"[RAG] run_retrieve_rules returned in {_time.time()-_t_rag:.2f}s — result={rag_rules}")
                if rag_rules:
                    topics_found = list(rag_rules.get("rules", {}).keys())
                    self.log(f"[RAG] Rules retrieved for topics: {topics_found}")
        except Exception as _rag_err:
            self.log(f"[RAG] FAILED — {type(_rag_err).__name__}: {_rag_err}")
            if tracker: tracker.report(f"⚠️ Authority code retrieval failed, using static rules. ({_rag_err})")
            rag_rules = None
        # --- END RAG INTEGRATION ---

        if _c_lift or _c_fire or _c_struct or rag_rules:
            compliance_text = "\nAUTHORITY COMPLIANCE RULES (MANDATORY — embed values used into manifest compliance_parameters):\n"
            if _c_lift:
                compliance_text += "## Lift Engineering — BS EN 81-20 / CIBSE Guide D:\n"
                compliance_text += json.dumps(_c_lift, indent=2) + "\n"
            # Use dynamic RAG fire rules when available; fall back to static file otherwise
            if rag_rules:
                compliance_text += f"## Fire Safety (DYNAMIC RAG - {rag_rules.get('authority', 'SCDF')}):\n"
                compliance_text += json.dumps(rag_rules.get("rules", {}), indent=2) + "\n"
            elif _c_fire:
                compliance_text += "## Fire Safety — BS EN 81-72 / BS 9999 / Approved Doc B:\n"
                compliance_text += json.dumps(_c_fire, indent=2) + "\n"
            if _c_struct:
                compliance_text += "## Structural — Wall Thicknesses:\n"
                compliance_text += json.dumps(_c_struct, indent=2) + "\n"

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

        # OPTIMIZATION: Cache BIM state with 120s TTL (30s was too aggressive)
        import time
        now = time.time()
        refresh_needed = True

        if hasattr(self, "_cached_state") and hasattr(self, "_cache_time"):
            age = now - self._cache_time
            force_refresh = any(x in user_prompt.lower() for x in ["create", "delete", "clear", "wipe"])
            if age < 120.0 and not force_refresh:
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
                # Load persisted shell parametric state (shape, footprint_scale_overrides, etc.)
                try:
                    import os as _os
                    from revit_mcp.utils import get_log_path
                    _shell_path = _os.path.join(_os.path.dirname(get_log_path()), "last_shell_state.json")
                    if _os.path.exists(_shell_path):
                        with open(_shell_path) as _f:
                            _saved_shell = json.load(_f)
                        if _saved_shell:
                            state_text += f"\nEXISTING SHELL PARAMETERS: {json.dumps(_saved_shell)}"
                            state_text += (
                                "\nCRITICAL — SHELL MEMORY: The EXISTING SHELL PARAMETERS above define "
                                "the current building shape and per-floor scale pattern. You MUST carry "
                                "these values forward into your manifest unchanged UNLESS the user "
                                "explicitly asks to modify them. In particular, preserve 'shape', "
                                "'footprint_scale_overrides' (extending or merging for new floors), "
                                "'width', and 'length'."
                            )
                except Exception as _le:
                    self.log(f"Shell state load warning: {_le}")
                state_text += f"\nCRITICAL: Refer to PER-FLOOR BREAKDOWN for detailed queries. Preserve existing state unless asked to change."
                self._cached_state = state_text
                self._cache_time = now
            except Exception as e:
                self.log(f"Error gathering state: {e}")
                state_text = ""

        # 0. Intercept delete/clear/wipe requests — bypass Gemini entirely
        prompt_lower = user_prompt.lower().strip()
        delete_result = self._try_intercept_delete(prompt_lower)
        if delete_result is not None:
            return delete_result
            
        # 1. Generate Master Manifest (Fast-Track) with Agentic Loop
        thinking_budget = self._classify_prompt_thinking_budget(user_prompt)
        self.log("Step 2: Requesting building plan from Gemini AI (model: {}, thinking_budget: {})".format(client.model, thinking_budget))
        if tracker: tracker.report(f"Sending to Gemini AI ({client.model}) for geometric reasoning... This may take 10-20s.")

        current_prompt = DISPATCHER_PROMPT + presets_text + compliance_text + "\n" + state_text + "\nUser Request: " + user_prompt
        max_attempts = 3

        for attempt in range(max_attempts):
            self.log(f"--- Orchestration Attempt {attempt + 1}/{max_attempts} ---")

            ai_start = time.time()
            manifest_json = client.generate_content(current_prompt, thinking_budget=thinking_budget)
            ai_duration = time.time() - ai_start
            
            self.log("Manifest received from AI. (Time: {:.2f}s). Parsing...".format(ai_duration))
            if tracker:
                tracker.report(f"AI response (Attempt {attempt+1}) in {ai_duration:.1f}s.")
                
            # Stream Intent and Resolution Thoughts to UI
            self._stream_narrative_to_user(manifest_json, tracker)
            
            try:
                self.log("_orchestrate: manifest_json head={}".format(repr(manifest_json[:300])))
                manifest_str = self._extract_json(manifest_json)
                manifest = json.loads(manifest_str)
                
                # UNWRAP AI TOOL-CALL-STYLE WRAPPERS
                for wrapper in ["orchestrate_build", "edit_entire_building_dimensions"]:
                    if wrapper in manifest and len(manifest) == 1:
                        manifest = manifest[wrapper]
                        break
                
                # CHECK FOR QUERY RESPONSE (Natural Language only)
                if "response" in manifest and not any(k in manifest for k in ["project_setup", "levels", "shell"]):
                    self.log("Dispatcher: AI detected a QUESTION. Returning natural language response.")
                    return str(manifest["response"])

                # Log full manifest to runner/console for debugging
                self.log("=== BUILDING MANIFEST ===\n{}\n=== END MANIFEST ===".format(
                    json.dumps(manifest, indent=2)))

                # Report design parameters and compliance numbers to chat
                self._report_design_parameters(manifest, presets, tracker)

                # EXECUTE BUILD (Validate/Build)
                def main_action():
                    import Autodesk.Revit.DB as DB # type: ignore
                    doc = uiapp.ActiveUIDocument.Document
                    workers = RevitWorkers(doc, tracker=tracker)
                    return workers.execute_fast_manifest(manifest)

                self.log(f"Attempting build execution for Attempt {attempt + 1}...")
                results = mcp_event_handler.run_on_main_thread(main_action)
                
                # CHECK FOR CONFLICTS
                if isinstance(results, dict) and results.get("status") == "CONFLICT":
                    conflict_desc = results.get("description", "Unknown Spatial Conflict")
                    self.log(f"CONFLICT DETECTED in Attempt {attempt+1}: {conflict_desc}")
                    if tracker:
                        tracker.report(f"### [Validation Failed] Attempt {attempt+1}\n{conflict_desc}")
                    
                    if attempt < max_attempts - 1:
                        current_prompt += f"\n\n[SPATIAL CONFLICT IN PREVIOUS ATTEMPT]:\n{conflict_desc}\n\nPlease resolve this conflict creatively in your next manifest. Explain your solution in the <resolution_thoughts> block."
                        continue
                    else:
                        self.log("Reached maximum orchestration attempts.")
                        return f"Failed to build after {max_attempts} attempts due to structural/spatial conflicts: {conflict_desc}"
                
                # SUCCESS: invalidate BIM state cache so next prompt picks up the new shell
                self._cache_time = 0

                # Return tracker report or summary
                if tracker:
                    tracker.analyze_manifest(manifest)
                    return tracker.generate_final_report(base_summary="Build Successful (Agentic Resolution Applied).")
                return "Build Completed successfully."

            except Exception as e:
                import traceback
                err = "Orchestration Error (Attempt {}): {}\n{}".format(attempt + 1, str(e), traceback.format_exc())
                self.log(err)
                if attempt == max_attempts - 1:
                    return err
                # If the failure was a missing JSON block (model produced prose instead),
                # give a laser-focused retry that forbids all prose output.
                if "Expecting value" in str(e) or "no JSON" in str(e).lower():
                    current_prompt += (
                        "\n\n[CRITICAL — PREVIOUS RESPONSE HAD NO JSON BLOCK]:\n"
                        "Your last response contained only prose/reasoning text — no ```json block was found.\n"
                        "THIS TIME: Output ONLY two things, nothing else:\n"
                        "1. <architectural_intent> block — 2 sentences MAX\n"
                        "2. The ```json\\n{...}\\n``` manifest block\n"
                        "Do NOT write any analysis, tables, bullet lists, or explanations outside these two blocks.\n"
                        "Use sparse footprint_scale_overrides (5-8 control points only, NOT one entry per floor)."
                    )
                else:
                    current_prompt += f"\n\n[ERROR IN PREVIOUS ATTEMPT]:\n{str(e)}\n\nPlease ensure you follow the JSON schema strictly."
                time.sleep(1)

    def _classify_prompt_thinking_budget(self, user_prompt):
        """Return Gemini thinking token budget based on prompt complexity.

        Simple edits (dimension changes, level count adjustments, minor overrides)
        get 8192 tokens — enough headroom without burning time.
        Full builds or complex multi-constraint requests get 16384.
        """
        import re
        p = user_prompt.lower()

        # Complex: new building, full regeneration, multi-constraint specification,
        # or any storey addition/extension (requires recalculating scale overrides for all floors)
        complex_keywords = [
            r"\bcreate\b", r"\bbuild\b", r"\bgenerate\b", r"\bnew building\b",
            r"\bfrom scratch\b", r"\bfire safety\b", r"\bbs en\b",
            r"\bmixed.use\b", r"\bcomplex\b", r"\bmulti.?storey\b",
            r"\badd.{0,20}stor", r"\bextend.{0,20}stor", r"\bmore.{0,20}stor",
            r"\badditional.{0,20}stor", r"\btaper\b", r"\bhourglass\b",
            r"\bfootprint.scale\b",
        ]
        for kw in complex_keywords:
            if re.search(kw, p):
                return 16384

        # Simple: single-dimension edits, height/width/floor changes, count tweaks
        return 8192

    def _stream_narrative_to_user(self, text, tracker):
        """Extracts and streams <architectural_intent> and <resolution_thoughts> to the user."""
        if not tracker: return

        import re
        intent_match = re.search(r"<architectural_intent>(.*?)</architectural_intent>", text, re.DOTALL)
        if intent_match:
            # Send as a single block — no per-line sleep (was adding up to 25s of dead wait)
            intent_text = intent_match.group(1).strip()
            if intent_text:
                tracker.report(f"**Architectural Intent:**\n{intent_text}", is_narrative=True)

        res_match = re.search(r"<resolution_thoughts>(.*?)</resolution_thoughts>", text, re.DOTALL)
        if res_match:
            res_text = res_match.group(1).strip()
            if res_text:
                tracker.report(f"**Conflict Resolution Logic:**\n{res_text}", is_narrative=True)

    def _try_intercept_delete(self, prompt_lower):
        """Detect delete/clear/wipe intent and execute directly, bypassing Gemini.
        Returns a result string if handled, or None to fall through to Gemini."""
        import re

        action_words = r"(?:delete|remove|clear|wipe|purge)"

        # --- "delete everything" / "delete all" / "clear the model" ---
        everything_patterns = [
            rf"{action_words}\s+(?:every\s*thing|everyth?ing|all\b(?!\s+\w))",  # "delete everything", "delete everyting", "delete all" (not "delete all walls")
            rf"{action_words}\s+the\s+(?:model|building|project)",
            rf"clean\s+the\s+(?:model|building|project)",
        ]
        for pat in everything_patterns:
            if re.search(pat, prompt_lower):
                self.log("Dispatcher: Delete-all intent detected. Bypassing Gemini.")
                from revit_mcp import tool_logic as logic
                result = mcp_event_handler.run_on_main_thread(logic.delete_all_elements_ui)
                self.log("Delete-all result: {}".format(result))
                return "Deleted {} elements from the model.".format(result.get("deleted_count", 0))

        # --- Partial delete: "delete all walls", "remove columns on floor 3-5" ---
        category_names = r"(?:walls?|floors?|slabs?|columns?|doors?|windows?|roofs?|stairs?|staircases?|railings?|grids?|levels?)"
        # Match: "delete [all] <category> [on/from floor/level X[-Y]]"
        partial_match = re.search(
            rf"{action_words}\s+(?:all\s+)?({category_names})(?:\s+(?:on|from|at)\s+(?:floor|level|storey)s?\s+(\d+)(?:\s*[-–to]+\s*(\d+))?)?",
            prompt_lower
        )
        if partial_match:
            category = partial_match.group(1)
            level_start = int(partial_match.group(2)) if partial_match.group(2) else None
            level_end = int(partial_match.group(3)) if partial_match.group(3) else None

            params = {"category": category, "level_start": level_start, "level_end": level_end}
            desc = "category='{}'".format(category)
            if level_start is not None:
                desc += ", floors {}-{}".format(level_start, level_end or level_start)

            self.log("Dispatcher: Partial delete detected ({}). Bypassing Gemini.".format(desc))
            from revit_mcp import tool_logic as logic
            result = mcp_event_handler.run_on_main_thread(logic.delete_elements_by_filter_ui, params)
            self.log("Partial delete result: {}".format(result))
            if result.get("error"):
                return "Delete failed: {}".format(result["error"])
            return "Deleted {} {} elements.".format(result.get("deleted_count", 0), category)

        # --- "delete everything on floor 3" (no category, just level) ---
        level_only_match = re.search(
            rf"{action_words}\s+(?:everything|all)\s+(?:on|from|at)\s+(?:floor|level|storey)s?\s+(\d+)(?:\s*[-–to]+\s*(\d+))?",
            prompt_lower
        )
        if level_only_match:
            level_start = int(level_only_match.group(1))
            level_end = int(level_only_match.group(2)) if level_only_match.group(2) else None

            params = {"category": "", "level_start": level_start, "level_end": level_end}
            self.log("Dispatcher: Level-scoped delete detected (floors {}-{}). Bypassing Gemini.".format(level_start, level_end or level_start))
            from revit_mcp import tool_logic as logic
            result = mcp_event_handler.run_on_main_thread(logic.delete_elements_by_filter_ui, params)
            self.log("Level-scoped delete result: {}".format(result))
            if result.get("error"):
                return "Delete failed: {}".format(result["error"])
            return "Deleted {} elements on floor(s) {}-{}.".format(result.get("deleted_count", 0), level_start, level_end or level_start)

        return None

    def _report_design_parameters(self, manifest, presets, tracker):
        """Format and stream design parameters + compliance numbers to the chat UI."""
        if not tracker:
            return

        typology = manifest.get("typology", "default")
        preset   = presets.get(typology) or presets.get("default") or {}
        cp       = manifest.get("compliance_parameters", {})
        setup    = manifest.get("project_setup", {})
        shell    = manifest.get("shell", {})
        lifts    = manifest.get("lifts", {})
        stairs   = manifest.get("staircases", {})

        lines = []

        # ── Typology ──────────────────────────────────────────────────────────
        lines.append("**Typology:** `{}`".format(typology))

        # ── Manifest shell ────────────────────────────────────────────────────
        lines.append("\n**Manifest — Building Shell:**")
        lvls = setup.get("levels", "?")
        lh   = setup.get("level_height", "?")
        lines.append("  - Levels: {}, typical height: {}mm".format(lvls, lh))
        if shell.get("width") and shell.get("length"):
            lines.append("  - Footprint: {}mm × {}mm".format(shell["width"], shell["length"]))
        if shell.get("column_spacing"):
            lines.append("  - Column grid: {}mm".format(shell["column_spacing"]))
        if lifts.get("count"):
            lines.append("  - Lifts: {}".format(lifts["count"]))
        if stairs.get("count"):
            lines.append("  - Staircases: {}".format(stairs["count"]))

        # ── Design parameters from preset ─────────────────────────────────────
        bd  = preset.get("building_defaults", {})
        cl  = preset.get("core_logic", {})
        pr  = preset.get("program_requirements", {})
        col = preset.get("column_logic", {})
        if bd or cl or pr or col:
            lines.append("\n**Design Parameters (preset: `{}`):**".format(typology))
            if bd.get("typical_floor_height"):
                lines.append("  - Typical floor height: {}mm".format(bd["typical_floor_height"]))
            if bd.get("first_storey_floor_height"):
                lines.append("  - Ground floor height: {}mm".format(bd["first_storey_floor_height"]))
            if bd.get("clear_ceiling_height"):
                lines.append("  - Clear ceiling height: {}mm".format(bd["clear_ceiling_height"]))
            if col.get("span"):
                lines.append("  - Column span range: {}–{}mm".format(col["span"][0], col["span"][1]))
            if col.get("offset_from_edge"):
                lines.append("  - Column offset from edge: {}mm".format(col["offset_from_edge"]))
            if pr.get("minimum_distance_facade_to_core"):
                lines.append("  - Min facade-to-core depth: {}mm".format(pr["minimum_distance_facade_to_core"]))
            if pr.get("core_area_ratio"):
                lo, hi = pr["core_area_ratio"]
                lines.append("  - Core area ratio: {:.0f}–{:.0f}%".format(lo * 100, hi * 100))
            if pr.get("occupancy_load_factor"):
                lines.append("  - Occupancy load factor: {} m²/person".format(pr["occupancy_load_factor"]))
            if cl.get("lift_waiting_time"):
                lines.append("  - Target lift waiting time: {}s".format(cl["lift_waiting_time"]))
            if cl.get("lift_lobby_width"):
                lines.append("  - Lift lobby width: {}mm".format(cl["lift_lobby_width"]))
            if cl.get("lift_shaft_size"):
                sz = cl["lift_shaft_size"]
                lines.append("  - Lift shaft size: {}×{}mm".format(sz[0], sz[1]))
            if cl.get("fire_lobby_std_depth"):
                lines.append("  - Fire lobby std depth: {}mm".format(cl["fire_lobby_std_depth"]))
            sc = cl.get("staircase_spec", {})
            if sc:
                lines.append("  - Staircase: {}mm riser / {}mm tread / {}mm flight / {}mm landing".format(
                    sc.get("riser", "?"), sc.get("tread", "?"),
                    sc.get("width_of_flight", "?"), sc.get("landing_width", "?")))

        # ── Authority compliance parameters (embedded in manifest) ─────────────
        if cp:
            lines.append("\n**Authority Compliance Parameters (manifest `compliance_parameters`):**")
            _labels = {
                "max_travel_distance_mm":    ("Max travel distance",  "mm"),
                "stair_riser_mm":            ("Stair riser",          "mm"),
                "stair_tread_mm":            ("Stair tread",          "mm"),
                "stair_flight_width_mm":     ("Stair flight width",   "mm"),
                "stair_landing_width_mm":    ("Stair landing width",  "mm"),
                "stair_headroom_mm":         ("Stair headroom",       "mm"),
                "stair_overrun_mm":          ("Stair overrun",        "mm"),
                "fire_lobby_min_area_mm2":   ("Fire lobby min area",  "m²"),
                "smoke_lobby_min_area_mm2":  ("Smoke lobby min area", "m²"),
                "smoke_lobby_min_depth_mm":  ("Smoke lobby min depth","mm"),
                "fire_lift_car_size_mm":     ("Fire lift car size",   "mm"),
                "lift_wall_thickness_mm":    ("Lift shaft wall",      "mm"),
                "std_wall_thickness_mm":     ("Std wall thickness",   "mm"),
                "lift_speed_m_s":            ("Lift speed",           "m/s"),
                "lift_door_time_s":          ("Lift door time",       "s"),
                "lift_transfer_time_s":      ("Lift transfer time",   "s"),
                "lift_peak_demand_fraction": ("Peak demand fraction", ""),
                "lift_interval_s":           ("Lift interval period", "s"),
                "lift_occupants_per_lift":   ("Occupants per lift",   ""),
            }
            for k, v in cp.items():
                label, unit = _labels.get(k, (k, ""))
                # Convert mm² values to m² for readability
                if unit == "m²" and isinstance(v, (int, float)):
                    display = "{:.1f} m²".format(v / 1_000_000)
                elif unit:
                    display = "{} {}".format(v, unit)
                else:
                    display = str(v)
                lines.append("  - {}: {}".format(label, display))
        else:
            lines.append("\n*No `compliance_parameters` block in manifest — Gemini did not embed compliance values.*")

        tracker.report("\n".join(lines))

    def _extract_json(self, text):
        # Log tail of response to diagnose extraction failures
        self.log("_extract_json: text len={}, tail={}".format(
            len(text), repr(text[-200:]) if len(text) > 200 else repr(text)))

        # Only match explicit ```json fences (case-insensitive).
        # Do NOT match bare ``` fences — they may contain ASCII diagrams or tables.
        import re as _re
        fence_match = _re.search(r"```[Jj][Ss][Oo][Nn]\s*\n([\s\S]*?)```", text)
        if fence_match:
            candidate = fence_match.group(1).strip()
            if candidate and candidate.startswith("{"):
                self.log("_extract_json: extracted via ```json fence ({} chars)".format(len(candidate)))
                return candidate

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
                candidate = data[start:end+1].strip()
                self.log("_extract_json: extracted via brace search ({} chars)".format(len(candidate)))
                return candidate
        except:
            pass

        self.log("_extract_json: FAILED — no JSON found in response")
        return data.strip()

orchestrator = Orchestrator()
