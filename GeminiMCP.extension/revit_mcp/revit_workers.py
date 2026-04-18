# -*- coding: utf-8 -*-
# NOTE: Do NOT import Autodesk.Revit.DB at module level.
# Each method does its own local import on the correct thread.
from revit_mcp.gemini_client import client
from revit_mcp.bridge import mcp_event_handler
from revit_mcp.utils import (
    safe_num, mm_to_ft, sqmm_to_sqft, 
    safe_set_comment, get_location_line_param,
    get_random_dim, load_presets, ft_to_mm, disallow_joins
)
from . import lift_logic
from . import staircase_logic
from . import fire_safety_logic
import math


def _merge_void_rects(rects):
    """Merge OVERLAPPING axis-aligned rectangles into non-overlapping ones.
    Each rect is (x1, y1, x2, y2).
    Uses a small 1mm overlap threshold to avoid merging rectangles that are 
    merely touching (which happens often with clipped perimeter voids).
    """
    if len(rects) <= 1:
        return list(rects)
    
    # Use a small epsilon (1mm in feet) to ensure we ONLY merge if they actually overlap.
    # 1mm is approx 0.00328 ft.
    eps = 0.00328
    
    merged = list(rects)
    changed = True
    while changed:
        changed = False
        out = []
        used = [False] * len(merged)
        for i in range(len(merged)):
            if used[i]:
                continue
            ax1, ay1, ax2, ay2 = merged[i]
            for j in range(i + 1, len(merged)):
                if used[j]:
                    continue
                bx1, by1, bx2, by2 = merged[j]
                # Check actual OVERLAP (not just touching)
                if ax1 < bx2 - eps and ax2 > bx1 + eps and ay1 < by2 - eps and ay2 > by1 + eps:
                    ax1 = min(ax1, bx1)
                    ay1 = min(ay1, by1)
                    ax2 = max(ax2, bx2)
                    ay2 = max(ay2, by2)
                    used[j] = True
                    changed = True
            out.append((ax1, ay1, ax2, ay2))
            used[i] = True
        merged = out
    return merged


def _make_circle_footprint(radius_mm):
    """Generate a 4-arc footprint_points approximating a circle of given radius.
    Uses 4 cardinal points + arc mid-points at 45° intervals (CCW winding).
    The 3-point arc API requires the mid-point to lie ON the arc (not at chord mid)."""
    r = radius_mm
    m = radius_mm / math.sqrt(2.0)  # radius * cos(45deg) = point at 45deg on circle
    return [
        [0,  -r, {"mid_x":  m, "mid_y": -m}],   # south -> mid SE -> east
        [r,   0, {"mid_x":  m, "mid_y":  m}],   # east  -> mid NE -> north
        [0,   r, {"mid_x": -m, "mid_y":  m}],   # north -> mid NW -> west
        [-r,  0, {"mid_x": -m, "mid_y": -m}],   # west  -> mid SW -> south
    ]


def _make_ellipse_footprint(rx_mm, ry_mm):
    """4-arc footprint approximating an ellipse (semi-axes rx, ry)."""
    mx = rx_mm / math.sqrt(2.0)
    my = ry_mm / math.sqrt(2.0)
    return [
        [0,    -ry_mm, {"mid_x":  mx, "mid_y": -my}],
        [rx_mm, 0,     {"mid_x":  mx, "mid_y":  my}],
        [0,     ry_mm, {"mid_x": -mx, "mid_y":  my}],
        [-rx_mm, 0,    {"mid_x": -mx, "mid_y": -my}],
    ]


def _expand_shape_shorthand(shell):
    """Expand shell['shape'] into shell['footprint_points'] if not already present.
    Supports 'circle', 'ellipse', 'oval'. Modifies shell in-place and returns it."""
    shape = str(shell.get("shape", "")).lower().strip()
    if not shape or shell.get("footprint_points"):
        return shell  # nothing to do
    w = safe_num(shell.get("width"), 30000)
    l = safe_num(shell.get("length"), w)
    if shape == "circle":
        r = max(w, l) / 2.0
        shell["footprint_points"] = _make_circle_footprint(r)
    elif shape in ("ellipse", "oval"):
        shell["footprint_points"] = _make_ellipse_footprint(w / 2.0, l / 2.0)
    return shell


def _get_interpolated_scale(scale_overrides, level_1based):
    """Return the scale for a given 1-based level index, linearly interpolating
    between sparse control-point keys (e.g. {"1":1.1,"10":0.7,"20":1.1}).
    Levels outside the key range clamp to the nearest endpoint."""
    if not scale_overrides:
        return 1.0
    # Normalise all keys to int
    pts = {}
    for k, v in scale_overrides.items():
        try:
            pts[int(k)] = float(v)
        except (ValueError, TypeError):
            pass
    if not pts:
        return 1.0
    if level_1based in pts:
        return pts[level_1based]
    sorted_keys = sorted(pts.keys())
    if level_1based <= sorted_keys[0]:
        return pts[sorted_keys[0]]
    if level_1based >= sorted_keys[-1]:
        return pts[sorted_keys[-1]]
    # Find bracketing keys and lerp
    lo = max(k for k in sorted_keys if k < level_1based)
    hi = min(k for k in sorted_keys if k > level_1based)
    t = (level_1based - lo) / float(hi - lo)
    return pts[lo] + t * (pts[hi] - pts[lo])


def _scale_footprint(pts, scale):
    """Scale footprint_points about the origin [0,0] by a uniform scale factor.
    Arc mid-points (3rd element dicts with mid_x/mid_y) are scaled the same way."""
    result = []
    for pt in pts:
        new_pt = [pt[0] * scale, pt[1] * scale]
        if len(pt) > 2 and isinstance(pt[2], dict):
            ad = pt[2]
            new_pt.append({
                "mid_x": ad.get("mid_x", 0) * scale,
                "mid_y": ad.get("mid_y", 0) * scale,
            })
        result.append(new_pt)
    return result


class RevitWorkers:
    def __init__(self, doc, tracker=None):
        from .spatial_registry import SpatialRegistry
        self.doc = doc
        self.tracker = tracker
        self.spatial_registry = SpatialRegistry()

    def log(self, message):
        client.log(message)

    @staticmethod
    def _phase_msg(label, new_count, reused_count):
        """Build a progress message that says 'Created X ...' or 'Updated X ...' depending on the operation."""
        parts = []
        if new_count > 0:
            parts.append(f"Created {new_count}")
        if reused_count > 0:
            parts.append(f"Updated {reused_count}")
        if not parts:
            return f"No changes to {label}."
        return f"{' + '.join(parts)} {label}."

    def execute_fast_manifest(self, manifest):
        """High-speed execution using intent-based logic and state-aware updates"""
        self.log("--- execute_fast_manifest START ---")
        import Autodesk.Revit.DB as DB # type: ignore
        from revit_mcp.building_generator import get_model_registry # type: ignore
        doc = self.doc
        results = {"levels": [], "elements": [], "summary": {}}
        
        # 1. State Scan
        registry = get_model_registry(doc)
        self._registry_cache = registry  # Cache so sub-methods can access for lift count detection

        # Extract typology and compliance_params from manifest for downstream use
        _typology = manifest.get("typology", "")
        self._manifest_typology = _typology  # store so sub-methods can access
        self._compliance_params = manifest.get("compliance_parameters", {})
        
        # Track counts of elements handled/created/deleted
        reused = [0]
        created = [0]
        deleted = [0]
        
        # 1. NUCLEAR LOCKDOWN: Forcefully disjoint all walls before any build moves
        from revit_mcp.utils import nuclear_lockdown
        nuclear_lockdown(doc)
        
        tg = DB.TransactionGroup(doc, "AI Build: Fast Manifest")
        tg.Start()
        
        from revit_mcp.utils import setup_failure_handling
        
        # Track elements that actually changed for efficient re-join
        affected_elements = []
        
        try:
            # --- PHASE 1: LEVELS ---
            if self.tracker: self.tracker.report("Setting up building elevations...")
            t = DB.Transaction(doc, "AI Build: Levels")
            t.Start()
            setup_failure_handling(t, use_nuclear=True)
            c0, r0 = created[0], reused[0]
            elevations, current_levels = self._process_levels(manifest, registry, created, reused)
            lvl_new, lvl_reused = created[0] - c0, reused[0] - r0
            t.Commit()
            t.Dispose()

            if self.tracker:
                self.tracker.record_created("levels", len(current_levels))
                # Build detailed height summary
                heights_mm = []
                for ei in range(len(elevations) - 1):
                    heights_mm.append(round((elevations[ei+1] - elevations[ei]) * 304.8))
                height_desc = ""
                if heights_mm:
                    unique_h = sorted(set(heights_mm))
                    if len(unique_h) == 1:
                        height_desc = f", all at {unique_h[0]}mm"
                    else:
                        height_desc = f", heights: {min(heights_mm)}-{max(heights_mm)}mm"
                self.tracker.report(
                    f"{self._phase_msg('levels', lvl_new, lvl_reused)} "
                    f"({len(current_levels)} total, top elevation {elevations[-1]*304.8:.0f}mm{height_desc})"
                )
            results["levels"] = [str(l.Id.Value) for l in current_levels]
            floor_dims, shell = self._process_shell_dimensions(manifest, current_levels, registry)
            shell = _expand_shape_shorthand(shell)
            # Propagate any shape expansion back so downstream keys like has_footprint stay consistent
            if "footprint_points" in shell and "footprint_points" not in manifest.get("shell", {}):
                manifest.setdefault("shell", {})["footprint_points"] = shell["footprint_points"]

            # --- PHASE 2: VERTICAL CIRCULATION (Unified Core: Lifts + Staircases) ---
            # Pre-calculate core layout BEFORE opening a transaction — this is pure Python
            # (no Revit document writes). Keeping heavy calc inside a transaction was wasting
            # lock time and contributing to the overall build latency.
            import importlib
            importlib.reload(staircase_logic)
            importlib.reload(fire_safety_logic)
            self.log("Step 3a: Pre-calculating Unified Vertical Circulation layout...")
            if self.tracker: self.tracker.report("Calculating unified core layout (Lifts + Fire Safety)...")

            core_bounds_ft = None
            if "core_bounds" in manifest and manifest["core_bounds"]:
                self.log("Consolidation: Using pre-planned core bounds from manifest.")
                from revit_mcp.utils import mm_to_ft
                cb_list = manifest["core_bounds"]
                bx1 = min(cb[0] for cb in cb_list); by1 = min(cb[1] for cb in cb_list)
                bx2 = max(cb[2] for cb in cb_list); by2 = max(cb[3] for cb in cb_list)
                core_bounds_ft = (mm_to_ft(bx1), mm_to_ft(by1), mm_to_ft(bx2), mm_to_ft(by2))
                voids_mm = manifest.get("voids", [])
                u_voids_ft = []
                for (vx1, vy1, vx2, vy2) in voids_mm:
                    u_voids_ft.append((mm_to_ft(vx1), mm_to_ft(vy1), mm_to_ft(vx2), mm_to_ft(vy2)))
                self._stair_voids = u_voids_ft
                core_bounds = core_bounds_ft
            else:
                self._stair_voids = []
                core_bounds = self._expand_unified_vertical_circulation(manifest, current_levels, elevations, floor_dims, affected_elements, results)

            # Catch Spatial Conflicts from the pre-calculation
            if isinstance(core_bounds, dict) and core_bounds.get("status") == "CONFLICT":
                try: tg.RollBack()
                except: pass
                return core_bounds

            # Now open the transaction only for the actual enforce-disjoint write
            t = DB.Transaction(doc, "AI Build: Vertical Circulation")
            t.Start()
            setup_failure_handling(t, use_nuclear=True)
            c0, r0 = created[0], reused[0]
            core_new, core_reused = created[0] - c0, reused[0] - r0
            self._enforce_disjoint(affected_elements)
            t.Commit()
            t.Dispose()
            t = None

            # --- PHASE 2.5: PRE-EMPTIVE CLEANUP ---
            # Delete old AI staircases BEFORE shell build to prevent Revit native "auto-adjust" ghosting.
            # Must be outside ANY transaction because it starts its own.
            self._stair_fps_cache = self._cleanup_staircases(doc, getattr(self, '_stair_run_data', []), manifest)

            if self.tracker and (core_new or core_reused):
                self.tracker.report(
                    f"{self._phase_msg('core elements', core_new, core_reused)} "
                    f"(Unified Lifts, Stairs, and Fire Safety lobbies synced to core bounds)"
                )

            # --- PHASE 2.9: AGGRESSIVE PRE-CLEANUP ---
            # Collect ALL old AI element IDs first, then bulk-delete in one call.
            # The previous per-element doc.Delete() in a loop was slow; bulk deletion
            # via a single ICollection[ElementId] call is significantly faster.
            t_clean = DB.Transaction(doc, "AI Build: Pre-Cleanup")
            t_clean.Start()
            setup_failure_handling(t_clean, use_nuclear=True)

            from System.Collections.Generic import List as CsList  # type: ignore
            ids_to_delete = CsList[DB.ElementId]()
            cats = [DB.BuiltInCategory.OST_Walls, DB.BuiltInCategory.OST_Floors, DB.BuiltInCategory.OST_Columns, DB.BuiltInCategory.OST_StructuralColumns]
            for c in cats:
                for el in DB.FilteredElementCollector(doc).OfCategory(c).WhereElementIsNotElementType().ToElements():
                    try:
                        p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                        if p and p.HasValue and p.AsString().startswith("AI_"):
                            ids_to_delete.Add(el.Id)
                    except:
                        pass

            pre_del_count = ids_to_delete.Count
            if pre_del_count > 0:
                doc.Delete(ids_to_delete)

            t_clean.Commit()
            t_clean.Dispose()
            if pre_del_count > 0:
                self.log("Atomic Pre-Cleanup: bulk-deleted {} legacy AI elements for clean build.".format(pre_del_count))

            # --- PHASE 3: WALLS & FLOORS ---
            t = DB.Transaction(doc, "AI Build: Shell")
            t.Start()
            setup_failure_handling(t, use_nuclear=True)
            self.log("Step 3b: Processing Shell Walls and Floors...")
            if self.tracker: self.tracker.report("Processing shell walls and floors...")
            # footprint_points in shell = organic mode; shell handles it natively
            # suppress_shell only needed when AI uses granular walls[] with arcs AND no footprint_points
            has_footprint = bool(shell.get("footprint_points"))
            suppress_shell = (not has_footprint) and (
                manifest.get("suppress_shell", False) or any(
                    "mid_x" in w or "mid_y" in w or "mid" in w
                    for w in manifest.get("walls", [])
                )
            )
            c0, r0 = created[0], reused[0]
            if not suppress_shell:
                self._process_walls(current_levels, elevations, floor_dims, shell, registry, results, created, reused, affected_elements)
            walls_new, walls_reused = created[0] - c0, reused[0] - r0
            c0, r0 = created[0], reused[0]
            res_f = self._process_floors(current_levels, floor_dims, shell, registry, results, created, reused, affected_elements)
            if isinstance(res_f, dict) and res_f.get("status") == "CONFLICT":
                try: t.RollBack(); t.Dispose()
                except: pass
                try: tg.RollBack()
                except: pass
                return res_f
            expanded_slab_dims = res_f
            floors_new, floors_reused = created[0] - c0, reused[0] - r0
            c0, r0 = created[0], reused[0]
            self._process_parapets(current_levels, expanded_slab_dims, floor_dims, shell, registry, results, created, reused, affected_elements)
            walls_new += created[0] - c0
            walls_reused += reused[0] - r0
            if self.tracker:
                self.tracker.record_created("walls", walls_new)
                self.tracker.record_created("floors", floors_new)
                # Compute footprint range for detail
                min_w = min(d[0] for d in floor_dims)
                max_w_d = max(d[0] for d in floor_dims)
                min_l = min(d[1] for d in floor_dims)
                max_l_d = max(d[1] for d in floor_dims)
                if min_w == max_w_d and min_l == max_l_d:
                    fp_desc = f"{max_w_d/1000:.0f}m x {max_l_d/1000:.0f}m"
                else:
                    fp_desc = f"{min_w/1000:.0f}-{max_w_d/1000:.0f}m x {min_l/1000:.0f}-{max_l_d/1000:.0f}m"
                cant = shell.get("cantilever_depth", 0)
                cant_desc = f", cantilever {cant}mm" if cant else ""
                self.tracker.report(
                    f"{self._phase_msg('perimeter walls', walls_new, walls_reused)} "
                    f"(4 per level, footprint {fp_desc})"
                )
                self.tracker.report(
                    f"{self._phase_msg('floor slabs', floors_new, floors_reused)} "
                    f"({len(current_levels)} levels{cant_desc})"
                )
            self._enforce_disjoint(affected_elements)
            t.Commit()
            t.Dispose()

            # --- PHASE 4: STRUCTURE ---
            # Synthesis Run (enforce 1/3 rule) before processing columns
            presets = load_presets()
            _t = getattr(self, "_manifest_typology", "")
            preset = presets.get(_t) or presets.get("default") or presets.get("commercial_office", {})
            max_w = max(d[0] for d in floor_dims)
            max_l = max(d[1] for d in floor_dims)
            
            p_col_logic = preset.get("column_logic", {})
            dna_span = p_col_logic.get("span", [12000, 15000])
            dna_offset = safe_num(p_col_logic.get("offset_from_edge", 1000), 1000)
            
            res_w = self._synthesize_structural_grid(max_w, dna_span, dna_offset)
            if isinstance(res_w, dict) and res_w.get("status") == "CONFLICT":
                try: tg.RollBack()
                except: pass
                return res_w
            _, synth_span_w = res_w

            res_l = self._synthesize_structural_grid(max_l, dna_span, dna_offset)
            if isinstance(res_l, dict) and res_l.get("status") == "CONFLICT":
                try: tg.RollBack()
                except: pass
                return res_l
            _, synth_span_l = res_l
            
            # Store synthesized spans for _process_columns_and_grids
            self._synth_span_w = synth_span_w
            self._synth_span_l = synth_span_l

            t = DB.Transaction(doc, "AI Build: Structure")
            t.Start()
            setup_failure_handling(t, use_nuclear=True)
            self.log("Step 3c: Processing Structure with Core Alignment...")
            if self.tracker: self.tracker.report("Processing structural columns...")
            c0, r0 = created[0], reused[0]
            self._process_columns_and_grids(current_levels, elevations, floor_dims, expanded_slab_dims, shell, registry, results, created, reused, affected_elements, core_bounds)
            cols_new, cols_reused = created[0] - c0, reused[0] - r0
            if self.tracker:
                self.tracker.record_created("columns", cols_new)
                col_spacing = shell.get("column_spacing", 10000)
                offset_edge = shell.get("column_offset_from_edge", 500)
                # col_spacing may be a list [x_spacing, y_spacing] from the AI
                if isinstance(col_spacing, list):
                    spacing_str = "x".join(f"{v/1000:.0f}" for v in col_spacing) + "m"
                else:
                    spacing_str = f"{col_spacing/1000:.0f}m"
                self.tracker.report(
                    f"{self._phase_msg('structural columns', cols_new, cols_reused)} "
                    f"(grid spacing {spacing_str}, {offset_edge}mm from edge)"
                )
            t.Commit()
            t.Dispose()

            # --- PHASE 5: GRANULAR ---
            t = DB.Transaction(doc, "AI Build: Granular")
            t.Start()
            setup_failure_handling(t, use_nuclear=True)
            self.log("Step 4: Processing Granular Element Overrides...")
            # Count granular walls from manifest (lift core + staircase + user-defined)
            gran_wall_count = len(manifest.get("walls", []))
            gran_floor_count = len(manifest.get("floors", []))
            gran_col_count = len(manifest.get("granular_columns", []))
            if self.tracker and (gran_wall_count + gran_floor_count + gran_col_count) > 0:
                parts = []
                if gran_wall_count: parts.append(f"{gran_wall_count} walls (lift cores, staircases, dividers)")
                if gran_floor_count: parts.append(f"{gran_floor_count} floors (landings, custom slabs)")
                if gran_col_count: parts.append(f"{gran_col_count} columns")
                self.tracker.report(f"Processing granular elements: {', '.join(parts)}...")
            c0, r0 = created[0], reused[0]
            self._process_granular_walls(manifest, current_levels, registry, results, created, reused, affected_elements)
            gw_new, gw_reused = created[0] - c0, reused[0] - r0
            c0, r0 = created[0], reused[0]
            res_gf = self._process_granular_floors(manifest, current_levels, registry, results, created, reused, affected_elements)
            if isinstance(res_gf, dict) and res_gf.get("status") == "CONFLICT":
                try: t.RollBack(); t.Dispose()
                except: pass
                try: tg.RollBack()
                except: pass
                return res_gf
            gf_new, gf_reused = created[0] - c0, reused[0] - r0
            c0, r0 = created[0], reused[0]
            self._process_granular_columns(manifest, current_levels, registry, results, created, reused)
            gc_new, gc_reused = created[0] - c0, reused[0] - r0
            if self.tracker:
                parts = []
                if gw_new or gw_reused: parts.append(self._phase_msg("walls", gw_new, gw_reused))
                if gf_new or gf_reused: parts.append(self._phase_msg("floors", gf_new, gf_reused))
                if gc_new or gc_reused: parts.append(self._phase_msg("columns", gc_new, gc_reused))
                if parts:
                    self.tracker.report(f"Granular overrides: {' | '.join(parts)}")
            self._enforce_disjoint(affected_elements)
            t.Commit()
            t.Dispose()

            # --- PHASE 5.5: STAIR RUNS (requires own StairsEditScope) ---
            # Wrapped in try/except so stair failures NEVER nuke the building.
            ss = getattr(self, '_stair_summary', None)
            if self.tracker:
                if ss:
                    self.tracker.report(
                        f"Creating stair runs ({ss['count']} cores, "
                        f"{ss['flights_typical']} flights/floor, "
                        f"{ss['risers_per_flight']} risers/flight at {ss['riser']}mm rise, "
                        f"{ss['tread']}mm tread)..."
                    )
                else:
                    self.tracker.report("Processing stair runs...")
            try:
                self.log("Step 4a: Creating Stair Runs...")
                stair_runs_before = self.tracker.elements_created["stair_runs"] if self.tracker else 0
                self._create_stair_runs(current_levels, results)
                if self.tracker:
                    stair_runs_added = self.tracker.elements_created["stair_runs"] - stair_runs_before
                    if stair_runs_added > 0:
                        self.tracker.report(f"Created {stair_runs_added} stair runs across {len(current_levels)-1} storeys.")
            except Exception as stair_err:
                self.log("Stair phase failed (non-fatal): {}".format(stair_err))

            # --- PHASE 5.6: DOORS ---
            if self.tracker: self.tracker.report("Placing doors in staircases, lobbies and lift shafts...")
            try:
                t = DB.Transaction(doc, "AI Build: Doors")
                t.Start()
                from revit_mcp.utils import setup_failure_handling
                setup_failure_handling(t, use_nuclear=False)
                doors_placed = self._process_doors(current_levels, results)
                t.Commit()
                t.Dispose()
                if self.tracker and doors_placed:
                    self.tracker.report(f"Placed {doors_placed} doors (staircases, fire lobbies, lift shafts).")
            except Exception as door_err:
                import traceback
                self.log("Door phase failed (non-fatal): {}\n{}".format(door_err, traceback.format_exc()))
                try: t.RollBack()
                except: pass

            # --- PHASE 6: CLEANUP & DOC ---
            if self.tracker: self.tracker.report("Finalizing and cleaning up...")
            t = DB.Transaction(doc, "AI Build: Cleanup")
            t.Start()
            setup_failure_handling(t, use_nuclear=True)
            self._cleanup_registry(registry, results, deleted)
            self._generate_documentation(current_levels, elevations, floor_dims, max_w, max_l)
            t.Commit()
            t.Dispose()
                
            # Store for potential future cleanup
            self._affected_elements = [el for el in affected_elements if el and el.IsValidObject]
            
            tg.Assimilate()
            
            results["summary"] = {"reused": reused[0], "created": created[0], "deleted": deleted[0]}
            self.log("Fast-Track Summary: {}".format(results["summary"]))

            # Persist shell parametric state so the next prompt can reference the
            # current shape/footprint_scale_overrides without reverse-engineering geometry.
            try:
                import json as _json, os as _os
                from revit_mcp.utils import get_log_path
                _shell_to_save = {k: v for k, v in manifest.get("shell", {}).items()}
                # footprint_points is auto-generated from 'shape' — skip to keep file compact
                if _shell_to_save.get("shape") and "footprint_points" in _shell_to_save:
                    del _shell_to_save["footprint_points"]
                _shell_path = _os.path.join(_os.path.dirname(get_log_path()), "last_shell_state.json")
                with open(_shell_path, "w") as _f:
                    _json.dump(_shell_to_save, _f)
                self.log("Shell state saved to last_shell_state.json")
            except Exception as _se:
                self.log("Shell state save warning: {}".format(_se))

            return results
            
        except Exception as e:
            import traceback
            self.log("CRITICAL ERROR in execute_fast_manifest: {}\n{}".format(str(e), traceback.format_exc()))
            # Transaction is disposed on Error or Commit, so we only RollBack the TG if fails.
            tg.RollBack()
            return {"error": str(e)}

    def _batch_disable_existing_joins(self, doc, registry):
        """High-speed pass to ensure ALL walls in the document are disjoint."""
        import Autodesk.Revit.DB as DB # type: ignore
        from revit_mcp.utils import disallow_joins, setup_failure_handling
        
        # Scan ALL walls, not just AI walls, to prevent AI cells joining to user walls.
        all_walls = DB.FilteredElementCollector(doc).OfClass(DB.Wall).WhereElementIsNotElementType().ToElements()
        
        if len(all_walls) == 0: return
        
        t = DB.Transaction(doc, "AI Build: Initial Join Guard")
        t.Start()
        setup_failure_handling(t)
        count = 0
        for wall in all_walls:
            if wall and isinstance(wall, DB.Wall):
                disallow_joins(wall)
                count += 1
        t.Commit()
        t.Dispose()
        self.log("Initial Join Guard: Forcefully disjointed {} total walls in project.".format(count))

    def _enforce_disjoint(self, affected_elements):
        """Final safety pass to ensure no implicit joins were triggered."""
        from revit_mcp.utils import disallow_joins
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        for el in affected_elements:
            if el and el.IsValidObject:
                # 1. Wall-Wall Join suppression (Ends)
                if isinstance(el, DB.Wall):
                    disallow_joins(el)
                
                # 2. Force Unjoin from everything (Floors, Columns, etc.)
                try:
                    joined_ids = DB.JoinGeometryUtils.GetJoinedElements(doc, el)
                    for j_id in joined_ids:
                        target = doc.GetElement(j_id)
                        if target and DB.JoinGeometryUtils.AreElementsJoined(doc, el, target):
                            DB.JoinGeometryUtils.UnjoinGeometry(doc, el, target)
                except:
                    pass

    def _synthesize_structural_grid(self, target_dim, span_range, min_offset):
        """
        Synthesises an optimal structural grid span for the given building dimension.

        The 1/3 cantilever rule governs the PERIMETER overhang (facade → first
        column = min_offset), which is always satisfied for typical values.
        It does NOT govern the inner zone between the last bay column and the
        building centre — in a central-core building that zone is the core and
        no structural columns are placed there.

        Returns: (final_dim, final_span) — never returns CONFLICT.
        """
        if isinstance(span_range, (int, float)):
            min_s, max_s = float(span_range), float(span_range)
        elif isinstance(span_range, list) and len(span_range) >= 2:
            min_s, max_s = float(span_range[0]), float(span_range[1])
        else:
            min_s, max_s = 10000.0, 12000.0

        mid_s = (min_s + max_s) / 2.0
        target_half = target_dim / 2.0

        for n in range(1, 20):
            s_lo = target_half / (n + 0.5)
            s_hi = (target_half - min_offset) / n if n > 0 else max_s
            lo = max(min_s, s_lo)
            hi = min(max_s, s_hi)
            if lo <= hi + 1.0:
                return target_dim, hi

        return target_dim, mid_s

    def _process_levels(self, manifest, registry, created, reused):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        setup = manifest.get("project_setup", {})
        
        # Default Calculation
        existing_count = sum(1 for k in registry.keys() if k.startswith("AI_Level_"))
        default_levels = max(1, existing_count - 1)
        
        default_height = 4000
        if "AI_Level_1" in registry and "AI_Level_2" in registry:
            try:
                l1, l2 = doc.GetElement(registry["AI_Level_1"]), doc.GetElement(registry["AI_Level_2"])
                if l1 and l2: default_height = (l2.Elevation - l1.Elevation) * 304.8
            except: pass

        levels_val = setup.get("levels", setup.get("storeys", default_levels))
        height_val = setup.get("level_height", default_height)
        height_overrides = setup.get("height_overrides", {})

        # Expand range keys like '2-10' â†' {'2':v, '3':v, ..., '10':v}
        expanded_overrides = {}
        for k, v in height_overrides.items():
            k_str = str(k).strip()
            if '-' in k_str:
                try:
                    parts = k_str.split('-')
                    lo, hi = int(parts[0].strip()), int(parts[1].strip())
                    for n in range(lo, hi + 1):
                        expanded_overrides[str(n)] = v
                except Exception:
                    expanded_overrides[k_str] = v
            else:
                expanded_overrides[k_str] = v
        height_overrides = expanded_overrides
        self.log("[Build] height_overrides after expansion: {}".format(
            {k: v for k, v in list(height_overrides.items())[:10]}))

        if isinstance(levels_val, list):
            elevations = [mm_to_ft(e) for e in levels_val]
        else:
            count = int(safe_num(levels_val, 1))
            elevations = [0.0]
            curr = 0.0
            from revit_mcp import staircase_logic
            for i in range(1, count + 1):
                h_val = height_overrides.get(str(i), height_val)
                raw_h = get_random_dim(h_val, default_height, variation=0.15) if h_val == "random" else safe_num(h_val, default_height)
                h = staircase_logic.adjust_storey_height(raw_h, height_val, is_top_floor=False)
                if self.tracker and abs(h - raw_h) > 1.0:
                    from revit_mcp import staircase_logic as _sc
                    _rpf = _sc._risers_per_flight_typical(height_val, 150)
                    _total_risers = _sc._snap_risers(h, 150)
                    _num_flights = _sc._calc_num_flights(h, height_val, 150)
                    self.tracker.log_adjustment(
                        f"Level {i}: height adjusted {raw_h:.0f}mm -> {h:.0f}mm "
                        f"to ensure even flight count ({_num_flights} flights, "
                        f"{_rpf} risers/flight, {_total_risers} total risers at 150mm riser height). "
                        f"Dogleg stairs require even flights so landings align at each floor."
                    )
                curr += mm_to_ft(h)
                elevations.append(curr)
        
        existing_views = {v.GenLevel.Id for v in DB.FilteredElementCollector(doc).OfClass(DB.ViewPlan) if v.ViewType == DB.ViewType.FloorPlan and v.GenLevel}
        
        current_levels = []
        for i, elev in enumerate(elevations):
            tag = "AI_Level_" + str(i+1)
            name = "AI Level " + str(i+1)
            lvl = doc.GetElement(registry[tag]) if tag in registry else next((l for l in DB.FilteredElementCollector(doc).OfClass(DB.Level) if l.Name == name), None)
            
            if lvl:
                if abs(lvl.Elevation - elev) > 0.001: lvl.Elevation = elev
                reused[0] += 1
            else:
                lvl = DB.Level.Create(doc, elev)
                lvl.Name = name
                created[0] += 1
            
            safe_set_comment(lvl, tag)
            current_levels.append(lvl)
            
            if lvl.Id not in existing_views:
                vt = next((vft for vft in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType) if vft.ViewFamily == DB.ViewFamily.FloorPlan), None)
                if vt: DB.ViewPlan.Create(doc, vt.Id, lvl.Id)
        
        # ROBUSTNESS: Only preserve old AI levels when the manifest did NOT
        # provide an explicit level count (i.e. this is a partial/property edit,
        # not a storey-count change).  On contractions (50 â†' 5 floors) keeping
        # the surplus levels inflates current_levels â†' floor_dims â†' column grid,
        # producing columns for every old floor span instead of just the new 5.
        # _cleanup_registry handles the actual deletion of surplus elements.
        explicit_level_count = setup.get("levels") or setup.get("storeys")
        if not explicit_level_count:
            for tag, eid in registry.items():
                if tag.startswith("AI_Level_") and \
                        tag not in [f"AI_Level_{i+1}" for i in range(len(elevations))]:
                    lvl = doc.GetElement(eid)
                    if lvl:
                        current_levels.append(lvl)
                        reused[0] += 1

        return elevations, current_levels
    def _process_shell_dimensions(self, manifest, current_levels, registry):
        doc = self.doc
        shell = manifest.get("shell", {})
        # When the AI sets force_global_dimensions=true it means the user asked
        # for a full-building footprint change (e.g. "make it 80x100m").  Skip
        # the PRESERVE EXISTING model-reading so every floor -- including the ones
        # that already have walls -- adopts the new shell width/length.
        force_global = bool(shell.get("force_global_dimensions", False))
        w_def, l_def = 30000.0, 50000.0
        
        # 1. Base Dimension Logic:
        # Use explicit shell dimensions. If missing, try to infer from Level 1 
        # model state (most reliable reference for building-wide scale/position).
        m_w, m_l = shell.get("width"), shell.get("length")
        m_pos = shell.get("position", [0.0, 0.0])

        # INFERENCE from Level 1 if manifest hasn't provided building-wide scale
        if (m_w is None or m_l is None or shell.get("position") is None) and registry:
            w_tag_l1, l_tag_l1 = "AI_Wall_L1_S", "AI_Wall_L1_W"
            if w_tag_l1 in registry and l_tag_l1 in registry:
                w_el, l_el = doc.GetElement(registry[w_tag_l1]), doc.GetElement(registry[l_tag_l1])
                if w_el and l_el and hasattr(w_el.Location, "Curve") and hasattr(l_el.Location, "Curve"):
                    if m_w is None: m_w = w_el.Location.Curve.Length * 304.8
                    if m_l is None: m_l = l_el.Location.Curve.Length * 304.8
                    if shell.get("position") is None:
                        # South wall (S) is X-parallel; its midpoint X is the building center X.
                        # West wall (W) is Y-parallel; its midpoint Y is the building center Y.
                        p_s = w_el.Location.Curve.Evaluate(0.5, True)
                        p_w = l_el.Location.Curve.Evaluate(0.5, True)
                        m_pos = [p_s.X * 304.8, p_w.Y * 304.8]

        base_w = safe_num(m_w, w_def)
        base_l = safe_num(m_l, l_def)
        base_pos = [safe_num(m_pos[0], 0.0), safe_num(m_pos[1], 0.0)]
        shell["width"] = base_w
        shell["length"] = base_l
        shell["position"] = base_pos
        overrides = shell.get("floor_overrides", {})
        
        dims = []
        for i in range(len(current_levels)):
            lvl_idx = i + 1
            ov = overrides.get(str(lvl_idx), {})

            # 1. Start with explicit per-level override (highest priority)
            w = ov.get("width")
            l = ov.get("length")

            # 2. PRESERVE EXISTING: Infer from existing model geometry.
            #    Only runs when:
            #    - force_global_dimensions is NOT set, AND
            #    - the manifest has NO floor_overrides (pure property edit, not
            #      a dimension-change operation).
            #    When floor_overrides exist, the manifest is explicitly defining
            #    which floors change and which stay at the shell default.
            #    Reading old wall dims for non-overridden floors would lock them
            #    to stale values from prior builds (e.g. L11 stuck at 50x50
            #    after L2-L10 were changed to 50x50 in a previous operation).
            has_floor_overrides = bool(overrides)
            if not force_global and not has_floor_overrides and (w is None or l is None):
                w_tag, l_tag = f"AI_Wall_L{lvl_idx}_S", f"AI_Wall_L{lvl_idx}_W"
                f_tag = f"AI_Floor_L{lvl_idx}"

                if w_tag in registry and l_tag in registry:
                    w_el, l_el = doc.GetElement(registry[w_tag]), doc.GetElement(registry[l_tag])
                    if w_el and l_el and hasattr(w_el.Location, "Curve") and hasattr(l_el.Location, "Curve"):
                        if w is None: w = w_el.Location.Curve.Length * 304.8
                        if l is None: l = l_el.Location.Curve.Length * 304.8

                if (w is None or l is None) and f_tag in registry:
                    floor_el = doc.GetElement(registry[f_tag])
                    if floor_el and hasattr(floor_el, "get_BoundingBox"):
                        bb = floor_el.get_BoundingBox(None)
                        if bb:
                            if w is None: w = (bb.Max.X - bb.Min.X) * 304.8
                            if l is None: l = (bb.Max.Y - bb.Min.Y) * 304.8

            # 3. Fall back to global shell dimensions (for NEW levels only)
            dim_source = "override" if ov.get("width") or ov.get("length") else ("model" if w is not None else "shell")
            if w is None: w = shell.get("width")
            if l is None: l = shell.get("length")
            final_w = get_random_dim(w, base_w, variation=0.25)
            final_l = get_random_dim(l, base_l, variation=0.25)

            # Log transitions where dimensions differ from shell default
            if abs(final_w - base_w) > 1.0 or abs(final_l - base_l) > 1.0:
                self.log("[ShellDims] L{}: {}x{} (source: {}, shell default: {}x{})".format(
                    lvl_idx, final_w, final_l, dim_source, base_w, base_l))

            dims.append((final_w, final_l))
        return dims, shell

    def _process_walls(self, current_levels, elevations, floor_dims, shell, registry, results, created, reused, affected_elements):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        updated = []

        footprint_pts = shell.get("footprint_points")
        if footprint_pts:
            # --- ORGANIC MODE: walls from polygon/arc footprint, optionally scaled per level ---
            scale_overrides = shell.get("footprint_scale_overrides", {})
            for k, lvl in enumerate(current_levels):
                elev = elevations[k]
                scale = _get_interpolated_scale(scale_overrides, k + 1)
                level_pts = _scale_footprint(footprint_pts, scale) if scale != 1.0 else footprint_pts
                n = len(level_pts)
                for j in range(n):
                    seg_start = level_pts[j]
                    seg_end   = level_pts[(j + 1) % n]
                    p1 = DB.XYZ(mm_to_ft(seg_start[0]), mm_to_ft(seg_start[1]), elev)
                    p2 = DB.XYZ(mm_to_ft(seg_end[0]),   mm_to_ft(seg_end[1]),   elev)
                    if p1.DistanceTo(p2) < mm_to_ft(2.0): continue
                    arc_data = seg_start[2] if len(seg_start) > 2 else None
                    is_arc_seg = arc_data and isinstance(arc_data, dict)
                    if is_arc_seg:
                        pm = DB.XYZ(mm_to_ft(arc_data["mid_x"]), mm_to_ft(arc_data["mid_y"]), elev)
                        try:
                            curve = DB.Arc.Create(p1, p2, pm)
                        except Exception:
                            try:
                                curve = DB.Line.CreateBound(p1, p2)
                            except Exception:
                                continue
                            is_arc_seg = False
                    else:
                        curve = DB.Line.CreateBound(p1, p2)

                    tag = "AI_Wall_L{}_Seg{}".format(k + 1, j)
                    wall_id = registry.get(tag)
                    wall = doc.GetElement(wall_id) if wall_id else None
                    # Arc walls must be delete-and-recreated (can't change Line->Arc in place)
                    if is_arc_seg and wall and isinstance(wall, DB.Wall):
                        doc.Delete(wall.Id)
                        wall = None

                    if wall and isinstance(wall, DB.Wall):
                        w_curve = wall.Location.Curve
                        if not (w_curve.GetEndPoint(0).IsAlmostEqualTo(curve.GetEndPoint(0)) and
                                w_curve.GetEndPoint(1).IsAlmostEqualTo(curve.GetEndPoint(1))):
                            disallow_joins(wall)
                            try:
                                wall.Location.Curve = curve
                                affected_elements.append(wall)
                            except Exception:
                                doc.Delete(wall.Id)
                                wall = None
                        if wall:
                            disallow_joins(wall)
                            reused[0] += 1

                    if not wall or not isinstance(wall, DB.Wall):
                        wall = DB.Wall.Create(doc, curve, lvl.Id, False)
                        disallow_joins(wall)
                        safe_set_comment(wall, tag)
                        registry[tag] = wall.Id
                        created[0] += 1
                        affected_elements.append(wall)

                    # Height constraint: attach to next level or set explicit height
                    try:
                        if k < len(current_levels) - 1:
                            wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(current_levels[k + 1].Id)
                        else:
                            wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(DB.ElementId.InvalidElementId)
                            h_ft = elevations[k + 1] - elevations[k] if k < len(elevations) - 1 else mm_to_ft(1000)
                            p_h = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                            if p_h: p_h.Set(h_ft)
                    except: pass

                    updated.append(wall)
                    results["elements"].append(str(wall.Id.Value))
            return updated

        # --- RECTANGULAR MODE (default): 4 straight walls per level ---
        tags = ["AI_Wall_L{}_N", "AI_Wall_L{}_E", "AI_Wall_L{}_S", "AI_Wall_L{}_W"]

        for k, lvl in enumerate(current_levels):
            w_k, l_k = floor_dims[k]
            w_k_ft, l_k_ft = mm_to_ft(w_k), mm_to_ft(l_k)
            m_pos = shell.get("position", [0.0, 0.0])
            cx_ft, cy_ft = mm_to_ft(m_pos[0]), mm_to_ft(m_pos[1])
            pts = [DB.XYZ(cx_ft - w_k_ft/2, cy_ft - l_k_ft/2, 0),
                   DB.XYZ(cx_ft + w_k_ft/2, cy_ft - l_k_ft/2, 0),
                   DB.XYZ(cx_ft + w_k_ft/2, cy_ft + l_k_ft/2, 0),
                   DB.XYZ(cx_ft - w_k_ft/2, cy_ft + l_k_ft/2, 0)]

            for j in range(4):
                raw_p1 = pts[j]
                raw_p2 = pts[(j+1)%4]
                p1 = DB.XYZ(raw_p1.X, raw_p1.Y, elevations[k])
                p2 = DB.XYZ(raw_p2.X, raw_p2.Y, elevations[k])
                if p1.DistanceTo(p2) < mm_to_ft(2.0): continue
                line = DB.Line.CreateBound(p1, p2)

                tag = tags[j].format(k+1)
                wall_id = registry.get(tag)
                wall = doc.GetElement(wall_id) if wall_id else None
                is_changed = False
                if wall and isinstance(wall, DB.Wall):
                    w_curve = wall.Location.Curve
                    if not (w_curve.GetEndPoint(0).IsAlmostEqualTo(line.GetEndPoint(0)) and
                            w_curve.GetEndPoint(1).IsAlmostEqualTo(line.GetEndPoint(1))):
                        disallow_joins(wall)
                        wall.Location.Curve = line
                        is_changed = True
                    disallow_joins(wall)
                    reused[0] += 1
                else:
                    wall = DB.Wall.Create(doc, line, lvl.Id, False)
                    disallow_joins(wall)
                    safe_set_comment(wall, tag)
                    created[0] += 1
                    is_changed = True
                if is_changed:
                    affected_elements.append(wall)
                try:
                    if k < len(current_levels) - 1:
                        wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(current_levels[k+1].Id)
                    else:
                        wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(DB.ElementId.InvalidElementId)
                        h_ft = elevations[k+1] - elevations[k] if k < len(elevations)-1 else mm_to_ft(1000)
                        p = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                        if p: p.Set(h_ft)
                except: pass
                updated.append(wall)
                results["elements"].append(str(wall.Id.Value))
        return updated

    def _draw_wall(self, doc, p1, p2, lvl, wall_type, height, tag, registry, results, created, reused, affected_elements=[]):
        import Autodesk.Revit.DB as DB # type: ignore
        line = DB.Line.CreateBound(p1, p2)
        wall_id = registry.get(tag)
        wall = doc.GetElement(wall_id) if wall_id else None
        
        is_changed = False
        if wall and isinstance(wall, DB.Wall):
            w_curve = wall.Location.Curve
            if not (w_curve.GetEndPoint(0).IsAlmostEqualTo(line.GetEndPoint(0)) and \
                    w_curve.GetEndPoint(1).IsAlmostEqualTo(line.GetEndPoint(1))):
                # PRE-MOVE LOCK
                disallow_joins(wall)
                wall.Location.Curve = line
                is_changed = True
            # POST-MOVE RE-ENFORCE
            disallow_joins(wall)
            reused[0] += 1
        else:
            wall = DB.Wall.Create(doc, line, wall_type.Id, lvl.Id, height, 0, False, False)
            disallow_joins(wall)
            safe_set_comment(wall, tag)
            created[0] += 1
            is_changed = True
            
        if is_changed:
            affected_elements.append(wall)
        
        results["elements"].append(str(wall.Id.Value))
        return wall

    def _process_parapets(self, current_levels, expanded_slab_dims, floor_dims, shell, registry, results, created, reused, affected_elements):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc

        g_parapet_h = safe_num(shell.get("parapet_height"), None)
        overrides = shell.get("floor_overrides", {})
        footprint_pts = shell.get("footprint_points")
        scale_overrides = shell.get("footprint_scale_overrides", {})

        for k in range(len(current_levels)):
            lvl = current_levels[k]

            lvl_ov = overrides.get(str(k+1), {})
            p_h_mm = safe_num(lvl_ov.get("parapet_height", g_parapet_h), 1000)
            if p_h_mm <= 0: continue

            if footprint_pts:
                # Organic mode: parapets sit on the slab edge.
                # Slab scale = max(this level's scale, level below's scale) -- shelter rule.
                # Parapet is needed when the slab is larger than the wall above it.
                scale_here  = _get_interpolated_scale(scale_overrides, k + 1)
                scale_below = _get_interpolated_scale(scale_overrides, k) if k > 0 else 0.0
                slab_scale  = max(scale_here, scale_below)
                scale_above = _get_interpolated_scale(scale_overrides, k + 2) if k + 1 < len(current_levels) else 0.0
                if slab_scale <= scale_above:
                    continue  # wall above covers this slab edge -- no exposed parapet needed
                level_pts = _scale_footprint(footprint_pts, slab_scale) if slab_scale != 1.0 else footprint_pts
                n = len(level_pts)
                wall_type = DB.FilteredElementCollector(doc).OfClass(DB.WallType).FirstElement()
                for j in range(n):
                    seg_start = level_pts[j]
                    seg_end   = level_pts[(j + 1) % n]
                    p1 = DB.XYZ(mm_to_ft(seg_start[0]), mm_to_ft(seg_start[1]), 0)
                    p2 = DB.XYZ(mm_to_ft(seg_end[0]),   mm_to_ft(seg_end[1]),   0)
                    arc_data = seg_start[2] if len(seg_start) > 2 else None
                    tag = "AI_Parapet_L{}_Seg{}".format(k+1, j)
                    if arc_data and isinstance(arc_data, dict):
                        pm = DB.XYZ(mm_to_ft(arc_data["mid_x"]), mm_to_ft(arc_data["mid_y"]), 0)
                        try:
                            curve = DB.Arc.Create(p1, p2, pm)
                            wall_id = registry.get(tag)
                            existing = doc.GetElement(wall_id) if wall_id else None
                            if existing and isinstance(existing, DB.Wall):
                                doc.Delete(existing.Id)
                            wall = DB.Wall.Create(doc, curve, wall_type.Id, lvl.Id, mm_to_ft(p_h_mm), 0, False, False)
                            disallow_joins(wall)
                            safe_set_comment(wall, tag)
                            registry[tag] = wall.Id
                            created[0] += 1
                            affected_elements.append(wall)
                            results["elements"].append(str(wall.Id.Value))
                        except Exception:
                            pass
                    else:
                        if p1.DistanceTo(p2) < mm_to_ft(2.0): continue
                        self._draw_wall(doc, p1, p2, lvl, wall_type, mm_to_ft(p_h_mm), tag, registry, results, created, reused, affected_elements)
            else:
                # Rectangular mode: parapets only where slab overhangs the wall above
                slab_w, slab_l = expanded_slab_dims[k]
                w_above, l_above = (0, 0)
                if k < len(floor_dims):
                    w_above, l_above = floor_dims[k]

                if slab_w > w_above + 10 or slab_l > l_above + 10:
                    sw_ft, sl_ft = mm_to_ft(slab_w), mm_to_ft(slab_l)
                    wa_ft, la_ft = mm_to_ft(w_above), mm_to_ft(l_above)

                    pts = [DB.XYZ(-sw_ft/2.0, -sl_ft/2.0, 0),
                           DB.XYZ(sw_ft/2.0, -sl_ft/2.0, 0),
                           DB.XYZ(sw_ft/2.0, sl_ft/2.0, 0),
                           DB.XYZ(-sw_ft/2.0, sl_ft/2.0, 0)]

                    wall_type = DB.FilteredElementCollector(doc).OfClass(DB.WallType).FirstElement()
                    for i in range(4):
                        p1, p2 = pts[i], pts[(i+1)%4]
                        mid = (p1 + p2) / 2.0
                        is_exposed = abs(mid.X) > (wa_ft/2.0 + 0.1) or abs(mid.Y) > (la_ft/2.0 + 0.1)

                        if is_exposed:
                            tag = "AI_Parapet_L{}_{}".format(k+1, i)
                            self._draw_wall(doc, p1, p2, lvl, wall_type, mm_to_ft(p_h_mm), tag, registry, results, created, reused, affected_elements)

    def _process_floors(self, current_levels, floor_dims, shell, registry, results, created, reused, affected_elements):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        ftype = DB.FilteredElementCollector(doc).OfClass(DB.FloorType).FirstElement()
        if not ftype: return
        
        # 1. SIMPLE SHELTER RULE: slab[k] = max(floor_dims[k-1], floor_dims[k])
        # Each slab covers the max of its two adjacent storeys. No upward
        # cascade -- only transition levels get expanded slabs.
        expanded_slab_dims = []
        g_c_depth = shell.get("cantilever_depth")
        overrides = shell.get("floor_overrides", {})

        for k in range(len(current_levels)):
            w_below = floor_dims[k-1][0] if k > 0 else 0
            l_below = floor_dims[k-1][1] if k > 0 else 0
            w_here = floor_dims[k][0] if k < len(floor_dims) else 0
            l_here = floor_dims[k][1] if k < len(floor_dims) else 0

            # Cantilever for THIS level
            lvl_ov = overrides.get(str(k+1), {})
            c_val = lvl_ov.get("cantilever_depth", g_c_depth)
            c_depth = get_random_dim(c_val, 1500, variation=0.5) if c_val == "random" else safe_num(c_val, 0)

            # SIMPLE SHELTER: max of adjacent storeys (no cascade propagation)
            base_w = max(w_below, w_here)
            base_l = max(l_below, l_here)
            slab_w = base_w + (c_depth * 2)
            slab_l = base_l + (c_depth * 2)

            expanded_slab_dims.append((slab_w, slab_l))

        # Diagnostic: log floor dims at transition levels so missing floors can be debugged
        from .runner import log as _flog
        for k in range(len(current_levels)):
            fd_w = floor_dims[k][0] if k < len(floor_dims) else 0
            fd_l = floor_dims[k][1] if k < len(floor_dims) else 0
            es_w, es_l = expanded_slab_dims[k]
            if k == 0 or fd_w != (floor_dims[k-1][0] if k > 0 else 0) or es_w != expanded_slab_dims[k-1][0]:
                _flog("[FloorDims] L{}: floor_dims=({:.0f},{:.0f}) expanded_slab=({:.0f},{:.0f})".format(
                    k+1, fd_w, fd_l, es_w, es_l))

        for k, lvl in enumerate(current_levels):
            slab_w, slab_l = expanded_slab_dims[k]
            if slab_w < 1000 or slab_l < 1000:
                continue  # shell suppressed (organic mode uses granular floors[])
            tag = "AI_Floor_L{}".format(k+1)

            footprint_pts = shell.get("footprint_points")
            m_pos = shell.get("position", [0.0, 0.0])
            cx_ft, cy_ft = mm_to_ft(m_pos[0]), mm_to_ft(m_pos[1])
            loop = DB.CurveLoop()
            if footprint_pts:
                # Organic footprint: build loop from polygon/arc points, scaled per level.
                # SHELTER RULE: slab scale = max(this level's scale, level below's scale)
                # so the slab always covers the larger of the two adjacent floor plates,
                # matching the rectangular mode's expanded_slab_dims max(below, here) logic.
                scale_overrides = shell.get("footprint_scale_overrides", {})
                scale_here  = _get_interpolated_scale(scale_overrides, k + 1)
                scale_below = _get_interpolated_scale(scale_overrides, k) if k > 0 else 0.0
                scale = max(scale_here, scale_below)
                level_pts = _scale_footprint(footprint_pts, scale) if scale != 1.0 else footprint_pts
                n = len(level_pts)
                for j in range(n):
                    seg_start = level_pts[j]
                    seg_end   = level_pts[(j + 1) % n]
                    fp1 = DB.XYZ(mm_to_ft(seg_start[0]), mm_to_ft(seg_start[1]), 0)
                    fp2 = DB.XYZ(mm_to_ft(seg_end[0]),   mm_to_ft(seg_end[1]),   0)
                    if fp1.DistanceTo(fp2) < mm_to_ft(2.0): continue
                    arc_data = seg_start[2] if len(seg_start) > 2 else None
                    if arc_data and isinstance(arc_data, dict):
                        fpm = DB.XYZ(mm_to_ft(arc_data["mid_x"]), mm_to_ft(arc_data["mid_y"]), 0)
                        try:
                            loop.Append(DB.Arc.Create(fp1, fp2, fpm))
                        except Exception:
                            try:
                                loop.Append(DB.Line.CreateBound(fp1, fp2))
                            except Exception:
                                pass
                    else:
                        try:
                            loop.Append(DB.Line.CreateBound(fp1, fp2))
                        except Exception:
                            pass
                # Bounding box for void-clipping: derive from scaled footprint points
                fp_xs = [p[0] for p in level_pts]
                fp_ys = [p[1] for p in level_pts]
                w_ft = mm_to_ft(max(fp_xs) - min(fp_xs))
                l_ft = mm_to_ft(max(fp_ys) - min(fp_ys))
                # Validate: if loop has fewer than 3 curves (skipped segments left gaps),
                # fall back to a bounding-box rectangle so the slab can still be created.
                if loop.NumberOfCurves() < 3:
                    from .runner import log as _flog3
                    _flog3("[FloorDims] L{}: organic loop degenerate ({} curves), using bbox fallback".format(k+1, loop.NumberOfCurves()))
                    loop = DB.CurveLoop()
                    fb_hw = w_ft / 2.0
                    fb_hl = l_ft / 2.0
                    fb1 = DB.XYZ(cx_ft - fb_hw, cy_ft - fb_hl, 0)
                    fb2 = DB.XYZ(cx_ft + fb_hw, cy_ft - fb_hl, 0)
                    fb3 = DB.XYZ(cx_ft + fb_hw, cy_ft + fb_hl, 0)
                    fb4 = DB.XYZ(cx_ft - fb_hw, cy_ft + fb_hl, 0)
                    loop.Append(DB.Line.CreateBound(fb1, fb2))
                    loop.Append(DB.Line.CreateBound(fb2, fb3))
                    loop.Append(DB.Line.CreateBound(fb3, fb4))
                    loop.Append(DB.Line.CreateBound(fb4, fb1))
            else:
                w_ft, l_ft = mm_to_ft(slab_w), mm_to_ft(slab_l)
                p1 = DB.XYZ(cx_ft - w_ft/2.0, cy_ft - l_ft/2.0, 0)
                p2 = DB.XYZ(cx_ft + w_ft/2.0, cy_ft - l_ft/2.0, 0)
                p3 = DB.XYZ(cx_ft + w_ft/2.0, cy_ft + l_ft/2.0, 0)
                p4 = DB.XYZ(cx_ft - w_ft/2.0, cy_ft + l_ft/2.0, 0)
                loop.Append(DB.Line.CreateBound(p1, p2))
                loop.Append(DB.Line.CreateBound(p2, p3))
                loop.Append(DB.Line.CreateBound(p3, p4))
                loop.Append(DB.Line.CreateBound(p4, p1))

            # Build the curve loop list: outer boundary + per-shaft void loops
            import System.Collections.Generic as Generic # type: ignore
            floor_loops = Generic.List[DB.CurveLoop]()
            floor_loops.Add(loop)
            # Combine lift voids + stair voids -- skip on ground floor (k == 0)
            shaft_voids = getattr(self, '_shaft_voids', None) or []
            stair_voids = getattr(self, '_stair_voids', None) or []
            all_voids = (list(shaft_voids) + list(stair_voids)) if k > 0 else []
            margin_ft = mm_to_ft(2.0)  # Make margin as small as possible so shaft boundaries align with edge
            slab_hx = w_ft / 2.0 - margin_ft
            slab_hy = l_ft / 2.0 - margin_ft
            if all_voids and slab_hx > 0 and slab_hy > 0:
                slab_min_x = cx_ft - slab_hx
                slab_max_x = cx_ft + slab_hx
                slab_min_y = cy_ft - slab_hy
                slab_max_y = cy_ft + slab_hy
                min_void_ft = mm_to_ft(200)
                clipped = []
                for (vx1, vy1, vx2, vy2) in all_voids:
                    cx1 = max(vx1, slab_min_x)
                    cy1 = max(vy1, slab_min_y)
                    cx2 = min(vx2, slab_max_x)
                    cy2 = min(vy2, slab_max_y)
                    if (cx2 - cx1) < min_void_ft or (cy2 - cy1) < min_void_ft:
                        continue
                    clipped.append((cx1, cy1, cx2, cy2))
                merged = _merge_void_rects(clipped)
                for (vx1, vy1, vx2, vy2) in merged:
                    vp1 = DB.XYZ(vx1, vy1, 0)
                    vp2 = DB.XYZ(vx1, vy2, 0)
                    vp3 = DB.XYZ(vx2, vy2, 0)
                    vp4 = DB.XYZ(vx2, vy1, 0)
                    void_loop = DB.CurveLoop()
                    void_loop.Append(DB.Line.CreateBound(vp1, vp2))
                    void_loop.Append(DB.Line.CreateBound(vp2, vp3))
                    void_loop.Append(DB.Line.CreateBound(vp3, vp4))
                    void_loop.Append(DB.Line.CreateBound(vp4, vp1))
                    floor_loops.Add(void_loop)

            # Old floors were pre-deleted in PHASE 2.9 -- always create fresh.
            # Force Regenerate at dimension transitions so Revit finalises
            # the previous floor's geometry before creating the next one.
            # Without this, Revit 2026 can carry forward the previous floor's
            # boundary to the new floor on transition levels.
            try:
                prev_slab = expanded_slab_dims[k-1] if k > 0 else (0, 0)
                if k > 0 and (abs(slab_w - prev_slab[0]) > 1 or abs(slab_l - prev_slab[1]) > 1):
                    doc.Regenerate()
                
                # Try create with voids
                try:
                    floor = DB.Floor.Create(doc, floor_loops, ftype.Id, lvl.Id)
                except Exception as e_void:
                    # Void loops invalid -- log actual error, retry without voids
                    from .runner import log as _flog2
                    _flog2("[FloorDims] L{}: void loops invalid ({}), creating floor without voids".format(k+1, e_void))
                    fallback_loops = Generic.List[DB.CurveLoop]()
                    fallback_loops.Add(loop)
                    floor = DB.Floor.Create(doc, fallback_loops, ftype.Id, lvl.Id)
            except Exception as e_final:
                self.log("[FloorDims] L{}: SKIPPED (loop invalid after bbox fallback: {})".format(k+1, str(e_final)[:200]))
                continue

            safe_set_comment(floor, tag)
            affected_elements.append(floor)
            registry[tag] = floor.Id

            if tag not in registry: created[0] += 1
            else: reused[0] += 1
            results["elements"].append(str(floor.Id.Value))
        
        return expanded_slab_dims

    def _process_columns_and_grids(self, current_levels, elevations, floor_dims, expanded_slab_dims, shell, registry, results, created, reused, affected_elements=[], core_bounds=None):
        """Structural column layout -- minimal columns, maximum span, core as structure.
        Rules:
        1. Core is structural -- no columns inside the core footprint.
        2. Maximize column span (use max of preset range) to minimize column count.
        3. Uniform grid from building center -- edge column only if cantilever > max_span.
        4. All columns inset by at least offset_from_edge from slab edge."""
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc

        # 1. Logic Setup: Span from shell -> existing model -> preset (use MAX for fewest columns)
        presets = load_presets()
        _t = getattr(self, "_manifest_typology", "")
        preset = presets.get(_t) or presets.get("default") or presets.get("commercial_office", {})
        p_col_logic = preset.get("column_logic", {})
        preset_span = p_col_logic.get("span", [12000, 15000])
        if isinstance(preset_span, list) and len(preset_span) >= 2:
            preset_span_max = float(preset_span[-1])
        elif isinstance(preset_span, (int, float)):
            preset_span_max = float(preset_span)
        else:
            preset_span_max = 15000.0

        # Use synthesized spans from synthesis run
        span_w_ft = mm_to_ft(getattr(self, '_synth_span_w', 12000))
        span_l_ft = mm_to_ft(getattr(self, '_synth_span_l', 12000))

        is_structural = True
        symbol = self._find_type(DB.BuiltInCategory.OST_StructuralColumns, shell.get("column_type", "Column"))
        if not symbol:
            # Fallback: grab first structural column family, then architectural
            symbol = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_StructuralColumns).OfClass(DB.FamilySymbol).FirstElement()
        if not symbol:
            symbol = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Columns).OfClass(DB.FamilySymbol).FirstElement()
            is_structural = False
        if not symbol: return
        if not symbol.IsActive: symbol.Activate()

        # Read offset_from_edge from shell or preset
        offset_from_edge_mm = safe_num(
            shell.get("column_offset", p_col_logic.get("offset_from_edge", 500)), 500)
        offset_from_edge_ft = mm_to_ft(offset_from_edge_mm)

        max_w = max(d[0] for d in floor_dims)
        max_l = max(d[1] for d in floor_dims)
        center_only = shell.get("columns_center_only", False) or "center area" in str(shell).lower()

        # 2. CORE-AWARE GRID: Columns are equally spaced between building edges
        #    and core walls. The grid is divided into regions by the lift core,
        #    and each region is subdivided with equal spans (minimizing column count
        #    while keeping span <= max_span). Staircases create additional 2D
        #    exclusion zones but don't affect the primary grid structure.
        import math as _math

        # Use the UNBUFFERED lift core bounds for grid definition (grid lines
        # align with actual core walls). Buffered exclusion zones are used for
        # 2D column culling only.
        exclusion_zones = getattr(self, '_core_exclusion_zones', [])
        if not exclusion_zones and core_bounds:
            exclusion_zones = [core_bounds]

        # Lift core bounds for grid computation -- use unbuffered bounds so grid
        # lines align with the actual core wall edges, not the column buffer.
        lift_core_ft = getattr(self, '_lift_core_bounds_ft', None)
        if lift_core_ft is None and exclusion_zones:
            lift_core_ft = exclusion_zones[0]

        m_pos = shell.get("position", [0.0, 0.0])
        cx_ft, cy_ft = mm_to_ft(m_pos[0]), mm_to_ft(m_pos[1])

        def compute_axis_grid(dim_mm, center_ft, core_min_ft, core_max_ft, span_ft):
            """Compute column grid positions along one axis."""
            half_dim_ft = mm_to_ft(dim_mm) / 2.0
            edge_pos = center_ft + (half_dim_ft - offset_from_edge_ft)
            edge_neg = center_ft - (half_dim_ft - offset_from_edge_ft)
            positions = set()

            full_dist = edge_pos - edge_neg
            if full_dist <= 0.1:
                positions.add(center_ft)
                return sorted(positions)

            # Single uniform grid: divide the full span into equal parts
            n_spans = max(1, int(_math.ceil(full_dist / span_ft - 0.001)))
            s = full_dist / n_spans
            for i in range(n_spans + 1):
                positions.add(round(edge_neg + i * s, 4))

            return sorted(positions)

        x_core_min = lift_core_ft[0] if lift_core_ft else None
        x_core_max = lift_core_ft[2] if lift_core_ft else None
        y_core_min = lift_core_ft[1] if lift_core_ft else None
        y_core_max = lift_core_ft[3] if lift_core_ft else None

        x_offsets = compute_axis_grid(max_w, cx_ft, x_core_min, x_core_max, span_w_ft)
        y_offsets = compute_axis_grid(max_l, cy_ft, y_core_min, y_core_max, span_l_ft)

        # Store grid positions for documentation (grid line creation)
        self._grid_x_offsets_ft = list(x_offsets)
        self._grid_y_offsets_ft = list(y_offsets)
        self._grid_cx_ft = cx_ft
        self._grid_cy_ft = cy_ft
        self._grid_max_w_ft = mm_to_ft(max_w) / 2.0
        self._grid_max_l_ft = mm_to_ft(max_l) / 2.0

        # Anchor for column tagging -- use lift core center (not merged core+stairs)
        if lift_core_ft:
            anchor_ft_x = (lift_core_ft[0] + lift_core_ft[2]) / 2.0
            anchor_ft_y = (lift_core_ft[1] + lift_core_ft[3]) / 2.0
        elif core_bounds:
            anchor_ft_x = (core_bounds[0] + core_bounds[2]) / 2.0
            anchor_ft_y = (core_bounds[1] + core_bounds[3]) / 2.0
        else:
            anchor_ft_x = cx_ft
            anchor_ft_y = cy_ft

        if center_only:
            half_w_ft = mm_to_ft(max_w) / 2.0
            half_l_ft = mm_to_ft(max_l) / 2.0
            x_offsets = [o for o in x_offsets if abs(o - cx_ft) < half_w_ft - 0.1]
            y_offsets = [o for o in y_offsets if abs(o - cy_ft) < half_l_ft - 0.1]

        # 3. PILLAR RULE with Stable Mapping
        col_margin_ft = mm_to_ft(400)

        max_level_for_grid = {}
        # Pre-build footprint polygon for point-in-polygon column culling
        _footprint_poly_mm = None
        _footprint_pts = shell.get("footprint_points")
        if _footprint_pts:
            import math as _math2
            poly_mm = []
            n_fp = len(_footprint_pts)
            for _j in range(n_fp):
                _s  = _footprint_pts[_j]
                _e  = _footprint_pts[(_j + 1) % n_fp]
                _x1, _y1 = float(_s[0]), float(_s[1])
                _x2, _y2 = float(_e[0]), float(_e[1])
                _arc = _s[2] if len(_s) > 2 else None
                if _arc and isinstance(_arc, dict):
                    _mx, _my = float(_arc["mid_x"]), float(_arc["mid_y"])
                    # Circumscribed circle from 3 points
                    _ax, _ay = _x2 - _x1, _y2 - _y1
                    _bx, _by = _mx - _x1, _my - _y1
                    _D = 2.0 * (_ax * _by - _ay * _bx)
                    if abs(_D) < 1e-6:
                        poly_mm.append((_x1, _y1))
                        continue
                    _ux = (_by * (_ax*_ax + _ay*_ay) - _ay * (_bx*_bx + _by*_by)) / _D
                    _uy = (_ax * (_bx*_bx + _by*_by) - _bx * (_ax*_ax + _ay*_ay)) / _D
                    _ccx, _ccy = _x1 + _ux, _y1 + _uy
                    _r = _math2.sqrt(_ux*_ux + _uy*_uy)
                    _a1 = _math2.atan2(_y1 - _ccy, _x1 - _ccx)
                    _a2 = _math2.atan2(_y2 - _ccy, _x2 - _ccx)
                    _am = _math2.atan2(_my - _ccy, _mx - _ccx)
                    # Determine winding direction through mid point
                    _PI2 = 2.0 * _math2.pi
                    _ccw = ((_am - _a1) % _PI2) < ((_a2 - _a1) % _PI2)
                    _span = (_a2 - _a1) % _PI2 if _ccw else (_a1 - _a2) % _PI2
                    _dir = 1 if _ccw else -1
                    for _si in range(24):
                        _t = _si / 24.0
                        _ang = _a1 + _dir * _span * _t
                        poly_mm.append((_ccx + _r * _math2.cos(_ang), _ccy + _r * _math2.sin(_ang)))
                else:
                    poly_mm.append((_x1, _y1))
            _footprint_poly_mm = poly_mm

        def _in_footprint(ox_ft, oy_ft):
            if _footprint_poly_mm is None:
                return True
            px, py = ft_to_mm(ox_ft), ft_to_mm(oy_ft)
            inside = False
            n_p = len(_footprint_poly_mm)
            for _i in range(n_p):
                _fx1, _fy1 = _footprint_poly_mm[_i]
                _fx2, _fy2 = _footprint_poly_mm[(_i + 1) % n_p]
                if ((_fy1 > py) != (_fy2 > py)) and (px < (_fx2 - _fx1) * (py - _fy1) / (_fy2 - _fy1) + _fx1):
                    inside = not inside
            return inside

        for ox in x_offsets:
            for oy in y_offsets:
                # Cull columns outside the organic footprint polygon
                if not _in_footprint(ox, oy):
                    continue

                # Cull columns inside ANY core/staircase footprint (inclusive with 1mm tolerance).
                # Tolerance ensures boundary-flush columns (offset_from_edge == wall thickness)
                # are correctly excluded even with floating-point rounding.
                _TOL_FT = mm_to_ft(1.0)
                inside_zone = False
                for zone in exclusion_zones:
                    if (zone[0] - _TOL_FT < ox < zone[2] + _TOL_FT) and (zone[1] - _TOL_FT < oy < zone[3] + _TOL_FT):
                        inside_zone = True
                        break
                if inside_zone:
                    continue

                # Indices for tagging: normalize to span relative to anchor
                ix = int(round((ox - anchor_ft_x) / span_w_ft))
                iy = int(round((oy - anchor_ft_y) / span_l_ft))

                k_highest = -1
                for k in range(len(current_levels)):
                    sw, sl = expanded_slab_dims[k]
                    # Organic footprint: containment already checked above; treat all levels as in-bounds
                    if _footprint_poly_mm is not None:
                        k_highest = k
                        continue
                    if abs(ox) <= (mm_to_ft(sw)/2.0 - col_margin_ft/2.0) and abs(oy) <= (mm_to_ft(sl)/2.0 - col_margin_ft/2.0):
                        k_highest = k
                if k_highest >= 0:
                    max_level_for_grid[(ix, iy, ox, oy)] = k_highest

        total_cols = sum(k_max for k_max in max_level_for_grid.values())
        self.log("Structure: {} column positions planned.".format(total_cols))

        # Delete ALL existing AI columns before recreating.  When the
        # building size or grid spacing changes, old columns at stale
        # positions would persist and create an irregular grid.
        new_tags = set()
        for (ix, iy, ox, oy), k_max in max_level_for_grid.items():
            for k in range(k_max):
                new_tags.add("AI_Col_L{}_GX{}_GY{}".format(k+1, ix, iy))

        existing_col_positions = {}
        del_count = 0
        try:
            for c in [DB.BuiltInCategory.OST_Columns, DB.BuiltInCategory.OST_StructuralColumns]:
                try:
                    for el in DB.FilteredElementCollector(doc).OfCategory(c).WhereElementIsNotElementType().ToElements():
                        comment = ""
                        try:
                            p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                            if p: comment = p.AsString() or ""
                        except: pass
                        if comment.startswith("AI_Col_"):
                            if comment not in new_tags:
                                # Stale column -- no longer in the grid
                                try:
                                    doc.Delete(el.Id)
                                    del_count += 1
                                except: pass
                            elif el.Location and hasattr(el.Location, "Point"):
                                pt = el.Location.Point
                                for li, lv in enumerate(current_levels):
                                    if el.LevelId == lv.Id:
                                        key = (round(pt.X, 2), round(pt.Y, 2), li)
                                        existing_col_positions[key] = el.Id
                                        break
                except Exception:
                    pass
        except Exception:
            pass
        if del_count > 0:
            self.log("Structure: deleted {} stale columns.".format(del_count))

        count = 0
        for (ix, iy, ox, oy), k_max in max_level_for_grid.items():
            for k in range(k_max):
                count += 1
                # Consolidated logging: only report major milestones to tracker
                if count % 500 == 0:
                    self.log("Step 3c Progress: Processed {}/{} columns...".format(count, total_cols))

                tag = "AI_Col_L{}_GX{}_GY{}".format(k+1, ix, iy)
                p = DB.XYZ(ox, oy, 0)
                lvl = current_levels[k]

                col = doc.GetElement(registry[tag]) if tag in registry else None
                if not col:
                    # Check spatial index -- skip if a column already exists here
                    pos_key = (round(ox, 2), round(oy, 2), k)
                    if pos_key in existing_col_positions:
                        # Reuse the existing column at this position
                        col = doc.GetElement(existing_col_positions[pos_key])
                        if col:
                            safe_set_comment(col, tag)
                            reused[0] += 1
                if not col:
                    st = DB.Structure.StructuralType.Column if is_structural else DB.Structure.StructuralType.NonStructural
                    col = doc.Create.NewFamilyInstance(p, symbol, lvl, st)
                    safe_set_comment(col, tag)
                    existing_col_positions[(round(ox, 2), round(oy, 2), k)] = col.Id
                    created[0] += 1
                else:
                    # OPTIMIZATION: Only update if position changed
                    if col.Location.Point.DistanceTo(p) > 0.001:
                        col.Location.Point = p
                    reused[0] += 1
                
                try:
                    bip = DB.BuiltInParameter.FAMILY_TOP_LEVEL_PARAM if is_structural else DB.BuiltInParameter.COLUMN_TOP_LEVEL_PARAM
                    param = col.get_Parameter(bip)
                    # OPTIMIZATION: Only update if level changed
                    if param and param.AsElementId() != current_levels[k+1].Id:
                        param.Set(current_levels[k+1].Id)
                except: pass
                
                results["elements"].append(str(col.Id.Value))
                affected_elements.append(col)

    def _find_core_wall_type(self, doc):
        """Return a WallType whose compound-structure thickness is closest to 350mm.
        Falls back to the first available WallType if none match within 100mm."""
        import Autodesk.Revit.DB as DB  # type: ignore
        best_type = None
        best_diff = float('inf')
        fallback  = None
        target_mm = 350.0
        for wt in DB.FilteredElementCollector(doc).OfClass(DB.WallType):
            if fallback is None:
                fallback = wt
            try:
                cs = wt.GetCompoundStructure()
                if cs:
                    width_mm = cs.GetWidth() * 304.8  # internal units → mm
                    diff = abs(width_mm - target_mm)
                    if diff < best_diff:
                        best_diff = diff
                        best_type = wt
            except:
                pass
        if best_type and best_diff <= 100.0:
            return best_type
        return fallback or best_type

    def _get_door_type(self, doc, min_width_mm=1000, category_hint=""):
        """Return the best available door FamilySymbol matching category_hint and ≥ min_width_mm.

        category_hint:
          "lift"        — prefer families whose name contains lift/elevator/shutter/sliding
          "single_leaf" — prefer families whose name contains single/solid/flush
          ""            — no preference, just find first with sufficient width
        """
        import Autodesk.Revit.DB as DB  # type: ignore
        syms = list(DB.FilteredElementCollector(doc)
                    .OfCategory(DB.BuiltInCategory.OST_Doors)
                    .OfClass(DB.FamilySymbol).ToElements())
        if not syms:
            return None

        hint = category_hint.lower()
        if hint == "lift":
            keywords = ["lift", "elevator", "shutter", "sliding"]
        elif hint == "single_leaf":
            keywords = ["single", "solid", "flush"]
        else:
            keywords = []

        def _name(sym):
            parts = []
            try: parts.append(sym.Name or "")
            except: pass
            try: parts.append(sym.Family.Name or "")
            except: pass
            return " ".join(parts).lower()

        def _width_ok(sym):
            try:
                wp = sym.get_Parameter(DB.BuiltInParameter.DOOR_WIDTH)
                if not wp:
                    wp = sym.LookupParameter("Width")
                if wp:
                    return wp.AsDouble() * 304.8 >= min_width_mm
            except:
                pass
            return True  # unknown width → accept

        # Pass 1: name match + width
        if keywords:
            for sym in syms:
                n = _name(sym)
                if any(kw in n for kw in keywords) and _width_ok(sym):
                    return sym
            # Pass 2: name match only (ignore width)
            for sym in syms:
                n = _name(sym)
                if any(kw in n for kw in keywords):
                    return sym

        # Pass 3: width match only
        for sym in syms:
            if _width_ok(sym):
                return sym

        return syms[0]

    def _find_wall_for_door(self, doc, pos_mm, level=None, max_dist_mm=600):
        """Find the wall whose centreline is closest to pos_mm at the given level."""
        import Autodesk.Revit.DB as DB  # type: ignore
        tol_ft = mm_to_ft(max_dist_mm)
        px, py  = mm_to_ft(pos_mm[0]), mm_to_ft(pos_mm[1])
        outline = DB.Outline(
            DB.XYZ(px - tol_ft, py - tol_ft, -1000.0),
            DB.XYZ(px + tol_ft, py + tol_ft,  1000.0)
        )
        bbf      = DB.BoundingBoxIntersectsFilter(outline)
        walls    = (DB.FilteredElementCollector(doc)
                    .OfClass(DB.Wall)
                    .WherePasses(bbf)
                    .ToElements())
        target   = DB.XYZ(px, py, 0.0)
        best_wall, best_dist = None, float('inf')
        for wall in walls:
            try:
                # Match level if provided
                if level and not self._wall_matches_level(wall, level):
                    continue
                loc = wall.Location
                if not isinstance(loc, DB.LocationCurve):
                    continue
                res = loc.Curve.Project(target)
                if res and res.Distance < best_dist:
                    best_dist = res.Distance
                    best_wall = wall
            except:
                pass
        return best_wall if best_dist < tol_ft else None

    def _wall_matches_level(self, wall, level):
        """Return True if wall's base constraint matches the given level.

        Uses .Value (Revit 2024+ / Revit 2026 API) to compare ElementId values.
        """
        import Autodesk.Revit.DB as DB  # type: ignore
        try:
            base_p = wall.get_Parameter(DB.BuiltInParameter.WALL_BASE_CONSTRAINT)
            if base_p:
                wall_lvl_id = base_p.AsElementId()
                # Revit 2024+ replaced IntegerValue with Value (Int64)
                try:
                    return wall_lvl_id.Value == level.Id.Value
                except AttributeError:
                    return wall_lvl_id.IntegerValue == level.Id.IntegerValue
        except:
            pass
        return False

    def _process_doors(self, current_levels, results):
        """Place doors in staircase, fire-lobby and lift-shaft walls (Phase 5.6)."""
        import Autodesk.Revit.DB as DB  # type: ignore
        doc       = self.doc
        door_data = getattr(self, '_door_data', [])
        if not door_data:
            self.log("Door phase: no door specs, skipping.")
            return 0

        # Build level map
        level_map = {l.Name: l for l in current_levels}
        for l in current_levels:
            p = l.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p and p.HasValue:
                level_map[p.AsString()] = l

        # Cache door symbols by category to avoid re-querying for every spec
        _door_sym_cache = {}
        def _get_sym(cat, width_mm):
            key = (cat, width_mm)
            if key not in _door_sym_cache:
                sym = self._get_door_type(doc, min_width_mm=width_mm, category_hint=cat)
                if sym and not sym.IsActive:
                    sym.Activate()
                    doc.Regenerate()
                _door_sym_cache[key] = sym
            return _door_sym_cache[key]

        # Pre-check that at least one door family is available
        if not self._get_door_type(doc, min_width_mm=0):
            self.log("Door phase: no door families loaded in project, skipping.")
            return 0

        # Refresh registry now — walls created in this build are not in the start-of-build cache
        try:
            from revit_mcp.building_generator import get_model_registry  # type: ignore
            registry = get_model_registry(doc)
            wall_keys = [k for k in registry if "Stair" in k or "LB" in k or "FL" in k]
            self.log("  Door registry: {} total, {} wall keys (sample: {})".format(
                len(registry), len(wall_keys), wall_keys[:3]))
        except Exception as _reg_err:
            self.log("  Door registry refresh failed: {}".format(_reg_err))
            registry = getattr(self, '_registry_cache', {})

        placed = 0
        for spec in door_data:
            spec_id       = spec.get("id", "AI_Door")
            pos_mm        = spec.get("position_mm")
            lvl_ids       = spec.get("levels", [])
            swing_in      = spec.get("swing_in", True)
            flip_hand      = spec.get("flip_hand", False)
            door_cat       = spec.get("door_category", "")
            min_w          = spec.get("min_width_mm", 1000)
            wall_ai_id_map = spec.get("wall_ai_id_map", {})
            swing_out_l1   = spec.get("swing_out_level1", False)
            if not pos_mm or not lvl_ids:
                continue

            door_sym = _get_sym(door_cat, min_w)
            if not door_sym:
                self.log("  Door {}: no symbol found, skipping spec.".format(spec_id))
                continue

            for li, level_id in enumerate(lvl_ids):
                level = level_map.get(level_id)
                if not level:
                    continue
                try:
                    # 1. Registry-first: look up wall by deterministic AI ID
                    wall = None
                    wall_ai_id = wall_ai_id_map.get(level_id) if wall_ai_id_map else None
                    if wall_ai_id and wall_ai_id in registry:
                        import Autodesk.Revit.DB as DB  # type: ignore
                        elem = doc.GetElement(registry[wall_ai_id])
                        if elem and isinstance(elem, DB.Wall):
                            wall = elem

                    # 2. Geometry fallback (level-constrained)
                    if not wall:
                        wall = self._find_wall_for_door(doc, pos_mm, level=level)
                    if not wall:
                        self.log("  Door {} @ level {}: no wall found".format(spec_id, level_id))
                        continue

                    ins_pt = DB.XYZ(
                        mm_to_ft(pos_mm[0]),
                        mm_to_ft(pos_mm[1]),
                        level.ProjectElevation
                    )
                    door_inst = doc.Create.NewFamilyInstance(
                        ins_pt, door_sym, wall, level,
                        DB.Structure.StructuralType.NonStructural
                    )
                    # Flip facing so external doors (and level-1 staircase doors) swing OUT
                    effective_swing_in = swing_in and not (li == 0 and swing_out_l1)
                    if not effective_swing_in:
                        try:
                            if door_inst.CanFlipFacing:
                                door_inst.FlipFacing()
                        except:
                            pass
                    # Flip hand so door leaf rests against the nearest perpendicular wall
                    if flip_hand:
                        try:
                            if door_inst.CanFlipHand:
                                door_inst.FlipHand()
                        except:
                            pass
                    safe_set_comment(door_inst, "{}_L{}".format(spec_id, li + 1))
                    results["elements"].append(str(door_inst.Id.Value))
                    placed += 1
                except Exception as _de:
                    self.log("  Door {} @ level {}: {}".format(spec_id, level_id, _de))

        self.log("Door phase complete: {} doors placed.".format(placed))
        return placed

    def _process_granular_walls(self, manifest, current_levels, registry, results, created, reused, affected_elements=[]):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        level_map = {l.Name: l for l in current_levels}
        for l in current_levels:
            p = l.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p and p.HasValue: level_map[p.AsString()] = l
        # Pre-fetch a 350mm WallType for core walls (user requirement); fall back to first available
        wall_type = self._find_core_wall_type(doc)

        granular_walls = []
        for w_data in manifest.get("walls", []):
            ai_id = w_data.get("id")
            if not ai_id: continue

            p1_raw = w_data.get("start", [0, 0, 0])
            p2_raw = w_data.get("end", [1000, 0, 0])
            p1 = DB.XYZ(mm_to_ft(p1_raw[0]), mm_to_ft(p1_raw[1]), 0)
            p2 = DB.XYZ(mm_to_ft(p2_raw[0]), mm_to_ft(p2_raw[1]), 0)

            # Arc wall: supported via mid_x/mid_y fields (or "mid": [x,y] list)
            mid_raw = w_data.get("mid") or (
                [w_data["mid_x"], w_data["mid_y"]] if "mid_x" in w_data and "mid_y" in w_data else None
            )
            is_arc = mid_raw is not None
            if is_arc:
                mx = mid_raw[0] if isinstance(mid_raw, (list, tuple)) else mid_raw.get("x", mid_raw.get("mid_x", 0))
                my = mid_raw[1] if isinstance(mid_raw, (list, tuple)) else mid_raw.get("y", mid_raw.get("mid_y", 0))
                pm = DB.XYZ(mm_to_ft(mx), mm_to_ft(my), 0)
                try:
                    curve = DB.Arc.Create(p1, p2, pm)
                except Exception as e:
                    self.log("  Arc wall {}: bad arc points ({}), falling back to line.".format(ai_id, e))
                    is_arc = False
                    curve = DB.Line.CreateBound(p1, p2)
            else:
                curve = DB.Line.CreateBound(p1, p2)

            lvl = level_map.get(w_data.get("level_id"))
            if not lvl: lvl = current_levels[0]

            existing = doc.GetElement(registry[ai_id]) if ai_id in registry else None

            # For arc walls: always delete and recreate — Revit does not support
            # changing a straight wall's Location.Curve to an Arc in-place.
            if is_arc and existing and isinstance(existing, DB.Wall):
                doc.Delete(existing.Id)
                existing = None

            wall = None
            is_changed = False
            if existing and isinstance(existing, DB.Wall):
                wall = existing
                w_curve = wall.Location.Curve
                def _xy_eq(a, b):
                    return abs(a.X - b.X) < 0.001 and abs(a.Y - b.Y) < 0.001
                wp0 = w_curve.GetEndPoint(0)
                wp1 = w_curve.GetEndPoint(1)
                cp0 = curve.GetEndPoint(0)
                cp1 = curve.GetEndPoint(1)
                same_fwd = _xy_eq(wp0, cp0) and _xy_eq(wp1, cp1)
                same_rev = _xy_eq(wp0, cp1) and _xy_eq(wp1, cp0)
                if not same_fwd and not same_rev:
                    disallow_joins(wall)
                    try:
                        wall.Location.Curve = curve
                        is_changed = True
                    except Exception as e:
                        doc.Delete(wall.Id)
                        wall = DB.Wall.Create(doc, curve, wall_type.Id, lvl.Id, 10.0, 0.0, False, False)
                        disallow_joins(wall)
                        safe_set_comment(wall, ai_id)
                        registry[ai_id] = wall.Id
                        is_changed = True
                if not is_changed:
                    disallow_joins(wall)
                reused[0] += 1
            else:
                wall = DB.Wall.Create(doc, curve, wall_type.Id, lvl.Id, 10.0, 0.0, False, False)
                disallow_joins(wall)
                safe_set_comment(wall, ai_id)
                registry[ai_id] = wall.Id
                created[0] += 1
                is_changed = True
            
            if is_changed:
                affected_elements.append(wall)
            
            # Height & Constraints
            h = w_data.get("height")
            if h:
                # Disconnect from top level if height is literal
                try:
                    p_top = wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE)
                    if p_top: p_top.Set(DB.ElementId.InvalidElementId)
                    p_h = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                    if p_h: p_h.Set(mm_to_ft(h))
                except: pass
            else:
                # Fallback: Try to connect to level above if not specified
                try:
                    current_idx = next((idx for idx, l in enumerate(current_levels) if l.Id == lvl.Id), -1)
                    if current_idx != -1 and current_idx < len(current_levels) - 1:
                        wall.get_Parameter(DB.BuiltInParameter.WALL_HEIGHT_TYPE).Set(current_levels[current_idx+1].Id)
                except: pass
            
            results["elements"].append(str(wall.Id.Value))
            granular_walls.append(wall)
        return granular_walls

    def _process_granular_floors(self, manifest, current_levels, registry, results, created, reused, affected_elements=[]):
        import Autodesk.Revit.DB as DB # type: ignore
        import System.Collections.Generic as Generic # type: ignore
        doc = self.doc
        level_map = {l.Name: l for l in current_levels}
        for l in current_levels:
            p = l.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p and p.HasValue: level_map[p.AsString()] = l
            
        ftype = DB.FilteredElementCollector(doc).OfClass(DB.FloorType).FirstElement()

        for f_data in manifest.get("floors", []):
            ai_id = f_data.get("id")
            if not ai_id: continue
            
            pts = f_data.get("points", [])
            if len(pts) < 3: continue
            
            loop = DB.CurveLoop()
            for i in range(len(pts)):
                p1_raw = pts[i]
                p2_raw = pts[(i+1)%len(pts)]
                p1 = DB.XYZ(mm_to_ft(p1_raw[0]), mm_to_ft(p1_raw[1]), 0)
                p2 = DB.XYZ(mm_to_ft(p2_raw[0]), mm_to_ft(p2_raw[1]), 0)
                loop.Append(DB.Line.CreateBound(p1, p2))
            
            lvl_name = f_data.get("level_id")
            lvl = level_map.get(lvl_name)
            if not lvl: lvl = current_levels[0]
            
            # Elevation Logic
            target_elev_mm = f_data.get("elevation")
            offset_ft = 0.0
            if target_elev_mm is not None:
                lvl_base_elev_mm = lvl.ProjectElevation * 304.8
                offset_ft = mm_to_ft(target_elev_mm - lvl_base_elev_mm)
            
            floor = doc.GetElement(registry[ai_id]) if ai_id in registry else None
            if floor and isinstance(floor, DB.Floor):
                doc.Delete(floor.Id)
            
            try:
                loops = Generic.List[DB.CurveLoop]()
                loops.Add(loop)
                floor = DB.Floor.Create(doc, loops, ftype.Id, lvl.Id)
                safe_set_comment(floor, ai_id)

                # Set Height Offset if needed
                if abs(offset_ft) > 0.001:
                    p_offset = floor.get_Parameter(DB.BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
                    if p_offset: p_offset.Set(offset_ft)

                created[0] += 1
                results["elements"].append(str(floor.Id.Value))
            except Exception as e:
                # Non-fatal: skip this landing slab and log — do NOT roll back the
                # transaction (which would also wipe lift-core walls and shell floors).
                self.log(f"GEOMETRY WARN (skipped) granular floor '{ai_id}': {e}")
                continue
        return None

    def _process_granular_columns(self, manifest, current_levels, registry, results, created, reused):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        level_map = {l.Name: l for l in current_levels}
        for l in current_levels:
            p = l.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p and p.HasValue: level_map[p.AsString()] = l
            
        symbol = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_StructuralColumns).OfClass(DB.FamilySymbol).FirstElement()
        if not symbol:
            symbol = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Columns).OfClass(DB.FamilySymbol).FirstElement()
        if not symbol: return
        if not symbol.IsActive: symbol.Activate()

        for c_data in manifest.get("columns", []):
            ai_id = c_data.get("id")
            if not ai_id: continue
            
            loc_raw = c_data.get("location", [0, 0, 0])
            p = DB.XYZ(mm_to_ft(loc_raw[0]), mm_to_ft(loc_raw[1]), mm_to_ft(loc_raw[2]))
            
            lvl = level_map.get(c_data.get("level_id"))
            if not lvl: lvl = current_levels[0]
            
            col = doc.GetElement(registry[ai_id]) if ai_id in registry else None
            if col and isinstance(col, DB.FamilyInstance):
                col.Location.Point = p
                reused[0] += 1
            else:
                st = DB.Structure.StructuralType.Column
                col = doc.Create.NewFamilyInstance(p, symbol, lvl, st)
                safe_set_comment(col, ai_id)
                created[0] += 1
            
            results["elements"].append(str(col.Id.Value))

    def _cleanup_registry(self, registry, results, deleted):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        touched = set(results["levels"]) | set(results["elements"])

        # Do not delete elements that were touched in this build (they are in `touched`).
        # There are no additional permanent exceptions — the old pattern of protecting
        # ALL "LiftR" and "Stair_" elements caused stale walls from a previous build to
        # survive cleanup when the staircase count or positions changed.
        def _is_protected_core_element(_el):
            return False  # touched-set check above already handles reused elements
        
        # 1. Broad Scan for any element with "AI_" comment that wasn't touched
        cats = [DB.BuiltInCategory.OST_Walls, DB.BuiltInCategory.OST_Floors, DB.BuiltInCategory.OST_Levels, 
                DB.BuiltInCategory.OST_Grids, DB.BuiltInCategory.OST_Columns, DB.BuiltInCategory.OST_StructuralColumns]
        import System.Collections.Generic as Generic # type: ignore
        net_cats = Generic.List[DB.BuiltInCategory]()
        for c in cats: net_cats.Add(c)
        filter = DB.ElementMulticategoryFilter(net_cats)
        
        # CRITICAL: Convert to list of IDs/Elements to avoid "Iterator Cannot Proceed" exception
        all_ai_elements = DB.FilteredElementCollector(doc).WherePasses(filter).WhereElementIsNotElementType().ToElements()
        
        # Collect all views for association check (Pre-Index by Level for speed)
        all_views = DB.FilteredElementCollector(doc).OfClass(DB.ViewPlan).ToElements()
        views_by_level = {}
        for v in all_views:
            if v.GenLevel:
                l_id = v.GenLevel.Id
                if l_id not in views_by_level: views_by_level[l_id] = []
                views_by_level[l_id].append(v)
        
        for el in list(all_ai_elements):
            if not el.IsValidObject: continue
            eid_str = str(el.Id.Value)
            if eid_str in touched: continue
            
            # Final check that it IS an AI element
            is_ai = False
            p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if not p: p = el.LookupParameter("Comments")
            if p and p.HasValue and p.AsString().startswith("AI_"):
                is_ai = True

            # Robustness: Also check Name for Levels and Views
            if not is_ai:
                if isinstance(el, DB.Level) and "AI Level" in el.Name: is_ai = True
                if hasattr(el, "Name") and "AI_" in el.Name: is_ai = True

            if is_ai:
                # NEVER delete protected core elements (lift / stair walls)
                if _is_protected_core_element(el):
                    continue
                # If it's a level, delete associated floor plans first
                if isinstance(el, DB.Level) and el.Id in views_by_level:
                    for v in views_by_level[el.Id]:
                        try:
                            if v.Pinned: v.Pinned = False
                            doc.Delete(v.Id)
                        except: pass
                
                try:
                    if hasattr(el, "Pinned") and el.Pinned: el.Pinned = False
                    doc.Delete(el.Id)
                    deleted[0] += 1
                except Exception as ex: 
                    # Silent skip, but we've tried our best
                    pass

    def _generate_documentation(self, current_levels, elevations, floor_dims, max_w, max_l):
        """Auto-create Revit Grid objects at the structural column grid positions.

        X-axis grids (vertical lines) are labelled A, B, C, ...
        Y-axis grids (horizontal lines) are labelled 1, 2, 3, ...
        """
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc

        x_offsets = getattr(self, '_grid_x_offsets_ft', [])
        y_offsets = getattr(self, '_grid_y_offsets_ft', [])
        if not x_offsets and not y_offsets:
            return

        half_w_ft = mm_to_ft(max_w) / 2.0
        half_l_ft = mm_to_ft(max_l) / 2.0
        # Grid lines extend slightly beyond the building for clarity
        overshoot_ft = mm_to_ft(3000)

        # Delete existing AI grids to prevent duplicates
        existing_grids = DB.FilteredElementCollector(doc).OfClass(DB.Grid).ToElements()
        for g in list(existing_grids):
            try:
                p = g.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if p and p.HasValue and "AI_Grid" in p.AsString():
                    doc.Delete(g.Id)
            except:
                pass

        # X-axis grids: vertical lines (constant X, varying Y) -- labelled A, B, C, ...
        for idx, ox in enumerate(x_offsets):
            label = ""
            n = idx
            while True:
                label = chr(ord('A') + n % 26) + label
                n = n // 26 - 1
                if n < 0:
                    break
            p1 = DB.XYZ(ox, -half_l_ft - overshoot_ft, 0)
            p2 = DB.XYZ(ox,  half_l_ft + overshoot_ft, 0)
            try:
                line = DB.Line.CreateBound(p1, p2)
                grid = DB.Grid.Create(doc, line)
                grid.Name = label
                safe_set_comment(grid, "AI_Grid_X_{}".format(idx))
            except Exception:
                pass

        # Y-axis grids: horizontal lines (constant Y, varying X) -- labelled 1, 2, 3, ...
        for idx, oy in enumerate(y_offsets):
            label = str(idx + 1)
            p1 = DB.XYZ(-half_w_ft - overshoot_ft, oy, 0)
            p2 = DB.XYZ( half_w_ft + overshoot_ft, oy, 0)
            try:
                line = DB.Line.CreateBound(p1, p2)
                grid = DB.Grid.Create(doc, line)
                grid.Name = label
                safe_set_comment(grid, "AI_Grid_Y_{}".format(idx))
            except Exception:
                pass

    def _expand_unified_vertical_circulation(self, manifest, current_levels, elevations, floor_dims, affected_elements=[], results=None):
        """
        Unified expansion for all core elements (Lifts, Stairs, Lobbies).
        Ensures a single cohesive layout and prevents spatial conflicts.
        """
        import Autodesk.Revit.DB as DB # type: ignore
        from revit_mcp.utils import mm_to_ft, load_presets, safe_num
        from revit_mcp import staircase_logic, fire_safety_logic, lift_logic
        
        setup = manifest.get("project_setup", {})
        typology = (manifest.get("typology") or setup.get("typology", "")).lower().replace(" ", "_")
        presets = load_presets()
        preset = presets.get(typology) or presets.get("default") or presets.get("commercial_office", {})
        
        # 1. Base Dimensions
        num_storeys = len(elevations) - 1
        # Use manifest level_height as the true typical floor height.
        # DO NOT derive from elevations[1]-elevations[0]: that is the first storey (often a
        # tall lobby, e.g. 8400mm), which is NOT representative of the typical floor.
        # When the wrong value is passed to get_stair_run_data, the Smart Detection there
        # overrides it with min(heights) — causing all regular floors to be computed against
        # the MINIMUM height (e.g. 3000mm), producing too many flights and low headroom.
        _preset_typical = safe_num(preset.get("floor_heights", {}).get("typical", 0), 0)
        _manifest_level_h = safe_num(setup.get("level_height", 0), 0)
        if _manifest_level_h > 0:
            typical_h_mm = _manifest_level_h
        elif _preset_typical > 0:
            typical_h_mm = _preset_typical
        elif len(elevations) > 2:
            typical_h_mm = round((elevations[2] - elevations[1]) * 304.8)  # skip lobby (floor 1)
        else:
            typical_h_mm = round((elevations[1] - elevations[0]) * 304.8) if len(elevations) > 1 else 4000
        
        # 2. Lift Requirements
        lifts_config = manifest.get("lifts", {})
        num_lifts = lifts_config.get("count")
        shell = manifest.get("shell", {})
        _fw = safe_num(shell.get("width", 30000), 30000)
        _fl_dim = safe_num(shell.get("length", 50000), 50000)
        _occ_density = safe_num(lifts_config.get("occupancy_density", 0.1), 0.1)
        _total_occ = max(100, (_fw * _fl_dim / 1e6) * _occ_density * num_storeys)
        _auto_lifts = lift_logic.calculate_lift_requirements(num_storeys, typical_h_mm, _total_occ)
        # AI may return count as a dict e.g. {"passenger": 4, "fire_fighting": 2}
        if isinstance(num_lifts, dict):
            num_lifts = num_lifts.get("passenger", num_lifts.get("total", num_lifts.get("count", None)))
        if num_lifts is None or num_lifts == "random":
            num_lifts = _auto_lifts
        else:
            try:
                num_lifts = min(int(num_lifts), _auto_lifts)
            except (TypeError, ValueError):
                num_lifts = _auto_lifts
        num_lifts = int(num_lifts)
        self._last_num_lifts = num_lifts # Cache for later
        
        lobby_w = lifts_config.get("lobby_width", preset.get("core_logic", {}).get("lift_lobby_width", 3000))
        self._last_lobby_w = lobby_w
        
        # 3. Fire Safety & Staircase Spec
        preset_fs = preset.get("core_logic", {}).get("fire_safety", {})
        stair_spec = preset_fs.get("staircase_spec", {}).copy()
        m_stair_config = manifest.get("staircases", {})
        m_spec = m_stair_config.get("spec", {})
        for k in ["riser", "tread", "width_of_flight", "landing_width"]:
            if k in m_spec: stair_spec[k] = safe_num(m_spec[k], stair_spec.get(k))
        # Only widen the entry (door-side) landing: 1000mm door + 1800mm clear = 2800mm.
        # The opposite landing stays at the preset width.
        stair_spec["entry_landing_width"] = max(stair_spec.get("landing_width", 1500), 2800)
        self._stair_spec = stair_spec

        # 4. Calculate Unified Layout
        f_center_mm = [0.0, 0.0] # Assume centered for building generator
        
        # Determine passenger lift bounds first for anchor
        layout_lifts = lift_logic.get_total_core_layout(num_lifts, lobby_width=lobby_w)
        lift_core_bounds_mm = (
            f_center_mm[0] - layout_lifts["total_w"]/2, 
            f_center_mm[1] - layout_lifts["total_d"]/2,
            f_center_mm[0] + layout_lifts["total_w"]/2,
            f_center_mm[1] + layout_lifts["total_d"]/2
        )
        
        safety_sets = fire_safety_logic.calculate_fire_safety_requirements(
            floor_dims, f_center_mm, lift_core_bounds_mm, 
            typical_h_mm, preset_fs, num_lifts, lobby_w
        )
        
        # 5. Build Manifest Data
        levels_manifest = []
        for i, elev_ft in enumerate(elevations):
            levels_manifest.append({"id": "AI Level {}".format(i+1), "elevation": elev_ft * 304.8})
            
        unified_man = fire_safety_logic.generate_fire_safety_manifest(
            safety_sets, levels_manifest, stair_spec, typical_h_mm, preset_fs, lift_core_bounds_mm, num_lifts, lobby_w
        )
        
        if isinstance(unified_man, dict) and unified_man.get("status") == "CONFLICT":
            return unified_man

        # Collect door specs from fire-safety manifest + passenger lift doors
        _door_specs = list(unified_man.get("door_specs", []))
        try:
            psgr_doors = lift_logic.get_passenger_lift_door_positions(
                num_lifts, center_pos=(f_center_mm[0], f_center_mm[1]),
                lobby_width=lobby_w, levels_data=levels_manifest
            )
            _door_specs.extend(psgr_doors)
        except Exception as _pd_e:
            self.log("Passenger lift door positions failed (non-fatal): {}".format(_pd_e))
        self._door_data = _door_specs

        # 5.5 Precise Spatial Reservation + Column Exclusion Zones
        # Use individual sub-boundary rects (not the merged core bounding rect) so
        # the column-exclusion logic culls only columns that actually fall inside a
        # core element — not everything inside the huge L-shaped merged bounding box.
        sub_bounds = unified_man.get("sub_boundaries", [])

        # Build per-element column exclusion zones (in ft) — pax lift core + every
        # fire-safety sub-boundary.  This replaces the old single merged rect that
        # wrongly excluded all internal columns when the layout had an L-shape.
        _excl = []
        _excl.append((
            mm_to_ft(lift_core_bounds_mm[0]), mm_to_ft(lift_core_bounds_mm[1]),
            mm_to_ft(lift_core_bounds_mm[2]), mm_to_ft(lift_core_bounds_mm[3])
        ))
        if sub_bounds:
            for sb in sub_bounds:
                rect = sb["rect"]
                res_id = sb["id"]
                _excl.append((mm_to_ft(rect[0]), mm_to_ft(rect[1]),
                               mm_to_ft(rect[2]), mm_to_ft(rect[3])))
                res, conflict = self.spatial_registry.reserve(res_id, (rect[0], rect[1], 0, rect[2], rect[3], typical_h_mm))
                if not res:
                    return {
                        "status": "CONFLICT",
                        "type": "GEOMETRIC_INTERFERENCE",
                        "description": "Spatial Conflict detected in Central Core: '{}' overlaps with '{}'".format(res_id, conflict)
                    }
        else:
            for i, cb in enumerate(unified_man.get("core_bounds", [])):
                res_id = "Core_Set_{}".format(i)
                self.spatial_registry.reserve(res_id, (cb[0], cb[1], 0, cb[2], cb[3], typical_h_mm))
                _excl.append((mm_to_ft(cb[0]), mm_to_ft(cb[1]), mm_to_ft(cb[2]), mm_to_ft(cb[3])))

        # Always add composite cluster bounds (core_bounds) as column exclusion zones.
        # sub_boundaries covers individual sub-components but each covers only part of
        # the lift bank width; core_bounds has the full-width cluster rect so columns
        # in the gap between the staircase east edge and the lift core east wall are culled.
        for cb in unified_man.get("core_bounds", []):
            cb_ft = (mm_to_ft(cb[0]), mm_to_ft(cb[1]), mm_to_ft(cb[2]), mm_to_ft(cb[3]))
            if cb_ft not in _excl:
                _excl.append(cb_ft)

        self._core_exclusion_zones = _excl
        # Lift-core bounds for column grid axis computation
        self._lift_core_bounds_ft = _excl[0]

        # 6. Inject into Build Pipeline
        if "walls" not in manifest: manifest["walls"] = []
        manifest["walls"].extend(unified_man.get("walls", []))
        
        if "floors" not in manifest: manifest["floors"] = []
        manifest["floors"].extend(unified_man.get("floors", []))
        
        # ── Passenger Lift Shaft Walls (previously missing!) ──────────────────
        # generate_lift_shaft_manifest produces the structural walls, internal
        # dividers, and top-cap slabs for every passenger lift shaft. These are
        # the actual lift car enclosure walls visible in plan and section.
        try:
            psgr_man = lift_logic.generate_lift_shaft_manifest(
                num_lifts,
                levels_manifest,
                center_pos=(f_center_mm[0], f_center_mm[1]),
                lobby_width=lobby_w
            )
            manifest["walls"].extend(psgr_man.get("walls", []))
            manifest["floors"].extend(psgr_man.get("floors", []))
            self.log("Passenger lift shaft: {} walls, {} floors generated.".format(
                len(psgr_man.get("walls", [])), len(psgr_man.get("floors", []))))
        except Exception as _psgr_e:
            self.log("Passenger lift shaft generation failed (non-fatal): {}".format(_psgr_e))

        # Voids
        u_voids_ft = []
        for (vx1, vy1, vx2, vy2) in unified_man.get("voids", []):
            u_voids_ft.append((mm_to_ft(vx1), mm_to_ft(vy1), mm_to_ft(vx2), mm_to_ft(vy2)))
        # Also add passenger lift shaft voids for floor slab cutouts
        try:
            psgr_void_rects = lift_logic.get_shaft_void_rectangles_mm(
                num_lifts, center_pos=(f_center_mm[0], f_center_mm[1]), lobby_width=lobby_w
            )
            for (vx1, vy1, vx2, vy2) in psgr_void_rects:
                u_voids_ft.append((mm_to_ft(vx1), mm_to_ft(vy1), mm_to_ft(vx2), mm_to_ft(vy2)))
        except Exception as _pv_e:
            self.log("Passenger lift void rects failed (non-fatal): {}".format(_pv_e))
        self._stair_voids = u_voids_ft
        
        # Combined Bounds (ft)
        bounds_ft = None
        for cb in unified_man.get("core_bounds", []):
            cb_ft = (mm_to_ft(cb[0]), mm_to_ft(cb[1]), mm_to_ft(cb[2]), mm_to_ft(cb[3]))
            if bounds_ft is None:
                bounds_ft = cb_ft
            else:
                bounds_ft = (min(bounds_ft[0], cb_ft[0]), min(bounds_ft[1], cb_ft[1]),
                             max(bounds_ft[2], cb_ft[2]), max(bounds_ft[3], cb_ft[3]))
        
        # 7. Prepare Stair Run Data (Required for internal staircase geometry)
        # Use the true staircase shaft centres returned by generate_fire_safety_manifest.
        # safety_set["pos"] is the entry point at the lift-core boundary, NOT the shaft
        # centre — using it directly places stair runs inside the lift core.
        stair_center_data = unified_man.get("stair_centers", [])
        if stair_center_data:
            positions = [(sc[0], sc[1]) for sc in stair_center_data]
            rotated_indices = [idx for idx, sc in enumerate(stair_center_data) if sc[2]]
        else:
            positions = [s["pos"] for s in safety_sets]
            rotated_indices = [idx for idx, s in enumerate(safety_sets) if s.get("rotated")]
        shaft_w_nat = staircase_logic.get_shaft_dimensions(typical_h_mm, stair_spec)[0]
        enc_w = shaft_w_nat
        
        stair_overrides = unified_man.get("stair_overrides", [])
        
        self._stair_run_data = staircase_logic.get_stair_run_data(
            positions, levels_manifest, enc_w, stair_spec, typical_h_mm, lift_core_bounds_mm,
            floor_dims_mm=floor_dims, num_lifts=num_lifts, lobby_width=lobby_w,
            rotated_indices=rotated_indices, base_y_overrides=stair_overrides
        )
        
        # Summary for tracker
        self._stair_summary = {
            "count": len(positions),
            "riser": stair_spec.get("riser", 150),
            "tread": stair_spec.get("tread", 280),
            "typical_h": typical_h_mm,
            "flights_typical": staircase_logic._calc_num_flights(typical_h_mm, typical_h_mm, 150),
            "risers_per_flight": staircase_logic._risers_per_flight_typical(typical_h_mm, 150)
        }
        
        return bounds_ft

    def _build_dogleg_in_scope(self, doc, stair_id, rd, base_lvl, current_levels,
                              tread_ft, hw, landing_ft, spec_riser_mm, spec_tread_mm,
                              _StairsRun, _StairsLanding, _StairsRunJust):
        """Build a standard 2-flight dogleg within a given Staircase scope."""
        import Autodesk.Revit.DB as DB # type: ignore
        f1 = rd['flight_1']
        f2 = rd['flight_2']
        p1s = DB.XYZ(mm_to_ft(f1['start'][0]), mm_to_ft(f1['start'][1]), base_lvl.ProjectElevation)
        p1e = DB.XYZ(mm_to_ft(f1['end'][0]),   mm_to_ft(f1['end'][1]),   base_lvl.ProjectElevation)
        p2s = DB.XYZ(mm_to_ft(f2['start'][0]), mm_to_ft(f2['start'][1]), base_lvl.ProjectElevation)
        p2e = DB.XYZ(mm_to_ft(f2['end'][0]),   mm_to_ft(f2['end'][1]),   base_lvl.ProjectElevation)
        
        _StairsRun.CreateStraightRun(doc, stair_id, DB.Line.CreateBound(p1s, p1e), _StairsRunJust.Center)
        _StairsRun.CreateStraightRun(doc, stair_id, DB.Line.CreateBound(p2s, p2e), _StairsRunJust.Center)
        
        # Automatic landing between runs
        runs = doc.GetElement(stair_id).GetStairsRuns()
        if runs.Count >= 2:
            _StairsLanding.CreateAutomaticLanding(doc, runs[0], runs[1])

    def _build_multipair_in_scope(self, doc, stair_id, rd, base_lvl, current_levels,
                                 tread_ft, hw, landing_ft, spec_riser_mm, spec_tread_mm,
                                 _StairsRun, _StairsLanding, _StairsRunJust):
        """Build a complex multi-pair stair (Tall Floor) within a given Staircase scope."""
        import Autodesk.Revit.DB as DB # type: ignore
        flight_list = rd.get('flight_list', [])
        num_pairs = rd.get('num_flight_pairs', 1)
        total_risers = sum(flight_list) if flight_list else num_pairs * 28
        
        floor_span_ft = rd['top_elev'] - rd['base_elev'] 
        # Note: use elev difference from rd instead of level elev to be robust
        floor_span_ft = mm_to_ft(floor_span_ft)
        base_z = base_lvl.ProjectElevation
        riser_h_ft = (floor_span_ft / total_risers) if total_risers else mm_to_ft(spec_riser_mm)
        
        dyn_landing_ft = mm_to_ft(rd.get("dyn_landing_w_mm", 1500))
        f1_data = rd['flight_1']
        f2_data = rd['flight_2']
        f1_cx = mm_to_ft(f1_data['start'][0])
        f2_cx = mm_to_ft(f2_data['start'][0])
        flight_y_start_ft = mm_to_ft(f1_data['start'][1])

        # Detect 180-degree rotation: after rotation f1 goes in -Y direction (south).
        # Use min/max so left/right X is correct regardless of which flight rotated to which side.
        f1_y_end = mm_to_ft(f1_data['end'][1])
        is_rotated = (f1_y_end < flight_y_start_ft)
        left_cx  = min(f1_cx, f2_cx)
        right_cx = max(f1_cx, f2_cx)
        land_x_left  = left_cx  - hw
        land_x_right = right_cx + hw

        current_base_z = base_z

        # Shaft back wall (far side from flight_y_start).
        # f1_y_end is the flight-zone end (already rotated); adding dyn_landing_ft
        # gives the inner face of the shaft back wall.  Pinning mid-landing to this
        # constant ensures no gap regardless of per-pair riser count.
        if is_rotated:
            shaft_back_y = f1_y_end - dyn_landing_ft
        else:
            shaft_back_y = f1_y_end + dyn_landing_ft

        pair_idx = 0
        while pair_idx < num_pairs and pair_idx * 2 + 1 < len(flight_list):
            a_risers = flight_list[pair_idx * 2]
            b_risers = flight_list[pair_idx * 2 + 1]

            # Run A — direction depends on rotation
            a_run_len = max(a_risers - 1, 1) * tread_ft
            p_as = DB.XYZ(f1_cx, flight_y_start_ft, current_base_z)
            if is_rotated:
                p_ae = DB.XYZ(f1_cx, flight_y_start_ft - a_run_len, current_base_z)
            else:
                p_ae = DB.XYZ(f1_cx, flight_y_start_ft + a_run_len, current_base_z)
            _StairsRun.CreateStraightRun(doc, stair_id, DB.Line.CreateBound(p_as, p_ae), _StairsRunJust.Center)

            mid_z = current_base_z + a_risers * riser_h_ft

            # Mid landing — spans from Run A end to the shaft back wall.
            # For pairs with fewer risers, Run A ends short of the max-riser flight zone;
            # extending to shaft_back_y fills the gap so no void appears at the back wall.
            if is_rotated:
                land_y_top = flight_y_start_ft - a_run_len  # near edge = end of Run A
                land_y_bot = shaft_back_y                   # far edge = shaft back wall
            else:
                land_y_bot = flight_y_start_ft + a_run_len  # near edge = end of Run A
                land_y_top = shaft_back_y                   # far edge = shaft back wall
            lp1 = DB.XYZ(land_x_left,  land_y_bot, mid_z)
            lp2 = DB.XYZ(land_x_right, land_y_bot, mid_z)
            lp3 = DB.XYZ(land_x_right, land_y_top, mid_z)
            lp4 = DB.XYZ(land_x_left,  land_y_top, mid_z)
            mid_loop = DB.CurveLoop()
            mid_loop.Append(DB.Line.CreateBound(lp1, lp2))
            mid_loop.Append(DB.Line.CreateBound(lp2, lp3))
            mid_loop.Append(DB.Line.CreateBound(lp3, lp4))
            mid_loop.Append(DB.Line.CreateBound(lp4, lp1))
            _StairsLanding.CreateSketchedLanding(doc, stair_id, mid_loop, mid_z - base_z)

            # Run B — returns toward the starting side from where Run A ended
            b_run_len = max(b_risers - 1, 1) * tread_ft
            top_z = mid_z + b_risers * riser_h_ft
            if is_rotated:
                p_bs = DB.XYZ(f2_cx, flight_y_start_ft - a_run_len, mid_z)
                p_be = DB.XYZ(f2_cx, flight_y_start_ft - a_run_len + b_run_len, mid_z)
            else:
                p_bs = DB.XYZ(f2_cx, flight_y_start_ft + a_run_len, mid_z)
                p_be = DB.XYZ(f2_cx, flight_y_start_ft + a_run_len - b_run_len, mid_z)
            _StairsRun.CreateStraightRun(doc, stair_id, DB.Line.CreateBound(p_bs, p_be), _StairsRunJust.Center)

            # Intermediate landing — connects Run B end to next pair's Run A start.
            # When b_risers < a_risers, Run B ends short of flight_y_start; the landing
            # is extended to cover that gap so the near-side shaft wall has no void.
            if pair_idx < num_pairs - 1:
                inter_z = top_z
                if is_rotated:
                    # Run B ends at: flight_y_start_ft - a_run_len + b_run_len
                    b_end_y = flight_y_start_ft - a_run_len + b_run_len
                    inter_far_y = min(flight_y_start_ft, b_end_y)  # southernmost cover
                    il1 = DB.XYZ(land_x_left,  inter_far_y, inter_z)
                    il2 = DB.XYZ(land_x_right, inter_far_y, inter_z)
                    il3 = DB.XYZ(land_x_right, flight_y_start_ft + dyn_landing_ft, inter_z)
                    il4 = DB.XYZ(land_x_left,  flight_y_start_ft + dyn_landing_ft, inter_z)
                else:
                    # Run B ends at: flight_y_start_ft + a_run_len - b_run_len
                    b_end_y = flight_y_start_ft + a_run_len - b_run_len
                    inter_far_y = max(flight_y_start_ft, b_end_y)  # northernmost cover
                    il1 = DB.XYZ(land_x_left,  flight_y_start_ft - dyn_landing_ft, inter_z)
                    il2 = DB.XYZ(land_x_right, flight_y_start_ft - dyn_landing_ft, inter_z)
                    il3 = DB.XYZ(land_x_right, inter_far_y, inter_z)
                    il4 = DB.XYZ(land_x_left,  inter_far_y, inter_z)
                iloop = DB.CurveLoop()
                iloop.Append(DB.Line.CreateBound(il1, il2))
                iloop.Append(DB.Line.CreateBound(il2, il3))
                iloop.Append(DB.Line.CreateBound(il3, il4))
                iloop.Append(DB.Line.CreateBound(il4, il1))
                _StairsLanding.CreateSketchedLanding(doc, stair_id, iloop, inter_z - base_z)

            current_base_z = top_z
            pair_idx += 1

    def _create_stair_runs(self, current_levels, results):
        """Create dogleg staircases. Force-reloads staircase_logic on every call."""
        # FORCE RELOAD: ensure freshly saved .py files are always used.
        # Revit caches modules in sys.modules; this bypasses stale .pyc state.
        import importlib
        import sys
        try:
            import revit_mcp.staircase_logic as _scl_mod
            importlib.reload(_scl_mod)
            self.log("Stair runs: reloaded staircase_logic ({}).".format(
                getattr(_scl_mod, '_BUILD_VERSION', 'unknown')))
        except Exception as _rl_e:
            self.log("Stair runs: reload warning: {}".format(_rl_e))

        import Autodesk.Revit.DB as DB  # type: ignore
        from revit_mcp.utils import setup_failure_handling
        from revit_mcp.preprocessors import NuclearJoinGuard
        import math
        import time
        import re
        doc = self.doc

        run_data_list = getattr(self, '_stair_run_data', [])
        if not run_data_list:
            self.log("Stair runs: no run data, skipping.")
            return

        try:
            _StairsEditScope = DB.StairsEditScope
            from Autodesk.Revit.DB.Architecture import (  # type: ignore
                StairsRun as _StairsRun,
                StairsLanding as _StairsLanding,
                StairsRunJustification as _StairsRunJust,
                MultistoryStairs as _MSS)
        except Exception as e:
            self.log("Stair runs: CANNOT load Stairs API: {}".format(e))
            return

    def _cleanup_staircases(self, doc, run_data_list, manifest):
        """Pre-emptive cleanup of AI staircase containers to prevent Revit auto-adjustment ghosting."""
        import Autodesk.Revit.DB as DB # type: ignore
        from revit_mcp.utils import setup_failure_handling
        
        manifest_fps = set()
        for rd in run_data_list:
            if 'fingerprint' in rd:
                manifest_fps.add("{}|{}".format(rd['tag'], rd['fingerprint']))

        t_del = DB.Transaction(doc, "AI Staircase Pre-Cleanup")
        t_del.Start()
        setup_failure_handling(t_del, use_nuclear=True)
        deleted_count = 0
        existing_fps = {} # Unconditional rebuild: always return empty

        ids_to_delete = []

        # 1. Cleanup old levels (always reset mid-levels to keep logic simple)
        for lvl in DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements():
            if lvl.Name.startswith("AI_Stair_Mid_"):
                ids_to_delete.append(lvl.Id)

        # 2. Total Nuclear Cleanup for ALL Vertical Circulation Containers
        # We delete ALL MultistoryStairs and ALL Stairs to ensure a 100% clean slate.
        # This prevents Revit's internal 'auto-adjustment' from preserving ghost stairs.
        for c in [DB.BuiltInCategory.OST_MultistoryStairs, DB.BuiltInCategory.OST_Stairs]:
            for el in DB.FilteredElementCollector(doc).OfCategory(c).WhereElementIsNotElementType().ToElements():
                try:
                    p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                    cmt = p.AsString() or "" if p else ""
                    name = el.Name or ""
                    # Aggressive: catch AI, Standard, or anything that looks like our managed stairs
                    if cmt.startswith("AI_") or "Stair" in name or not cmt:
                        if el.Id not in ids_to_delete:
                            ids_to_delete.append(el.Id)
                except: pass

        # 4. Atomic deletion pass
        for eid in ids_to_delete:
            try:
                doc.Delete(eid)
                deleted_count += 1
            except: pass

        t_del.Commit()
        t_del.Dispose()
        self.log("Staircase Nuclear Purge: {} deleted. Differential sync disabled for reliable generation.".format(deleted_count))
        return existing_fps


    def _create_stair_runs(self, current_levels, results):
        """Core assembly for dogleg/multi-pair staircases using StairsEditScope."""
        import Autodesk.Revit.DB as DB  # type: ignore
        from revit_mcp.utils import setup_failure_handling, mm_to_ft, ft_to_mm
        from revit_mcp.preprocessors import NuclearJoinGuard
        import re, math, time
        doc = self.doc
        run_data_list = getattr(self, '_stair_run_data', [])
        if not run_data_list:
            return

        try:
            _StairsEditScope = DB.StairsEditScope
            _MSS = DB.Architecture.MultistoryStairs
            from Autodesk.Revit.DB.Architecture import (  # type: ignore
                StairsRun as _StairsRun, 
                StairsLanding as _StairsLanding, 
                StairsRunJustification as _StairsRunJust
            )
        except Exception as e:
            self.log("Stair runs: CANNOT load Stairs API: {}".format(e))
            return

        spec = getattr(self, '_stair_spec', {})
        spec_riser_mm = spec.get("riser", 150)
        spec_tread_mm = spec.get("tread", 300)
        width_mm = spec.get("width_of_flight", 1500)
        landing_mm = spec.get("landing_width", 1500)
        tread_ft = mm_to_ft(spec_tread_mm)
        # EPSILON FIX: add a microscopic amount to avoid "Actual Tread < Min Tread" float warnings
        tread_ft += mm_to_ft(0.05) 
        hw = mm_to_ft(width_mm / 2.0)
        landing_ft = mm_to_ft(landing_mm)
        width_ft = mm_to_ft(width_mm)

        t_start = time.time()

        # --- Resolve risers-per-flight for this build ---
        rpf_typical = spec.get("_rpf", 14)
        try:
            from revit_mcp.staircase_logic import _risers_per_flight_typical
            rpf_typical = _risers_per_flight_typical(
                float(spec.get("_typical_h", 4200)), spec.get("riser", 150))
        except: pass

        pair_height_mm = rpf_typical * 2 * spec_riser_mm  # e.g. 14*2*150 = 4200mm
        pair_height_ft = mm_to_ft(pair_height_mm)

        # --- Create intermediate levels for multi-pair floors ---
        # Tall floors (e.g. 21000mm) get split into N identical 4200mm spans
        # via intermediate levels.  ALL spans become identical, enabling one
        # MultistoryStairs clone for the entire core.
        intermediate_levels = {}  # (base_level_idx, pair_index) -> Level
        t_mid = DB.Transaction(doc, "AI Stair Intermediate Levels")
        t_mid.Start()
        setup_failure_handling(t_mid, use_nuclear=True)
        for rd in run_data_list:
            n_intermediate = rd.get('_intermediate_count', 0)
            if n_intermediate <= 0:
                continue
            bi = rd['base_level_idx']
            if bi >= len(current_levels):
                continue
            base_elev = current_levels[bi].ProjectElevation
            import System.Collections.Generic as Generic # type: ignore
            for p in range(n_intermediate):
                key = (bi, p)
                if key in intermediate_levels:
                    continue
                intermediate_heights = rd.get('intermediate_heights_mm', [])
                if p < len(intermediate_heights):
                    mid_elev = base_elev + mm_to_ft(intermediate_heights[p])
                else:
                    mid_elev = base_elev + (p + 1) * pair_height_ft
                mid_lvl = DB.Level.Create(doc, mid_elev)
                mid_lvl.Name = "AI_Stair_Mid_{}_{}".format(bi + 1, p + 1)
                intermediate_levels[key] = mid_lvl

                import re
                tag_str = rd.get('tag', '')
                m_tag = re.match(r'AI_(Stair_\d+)_L\d+_Run', tag_str)
                c_tag = m_tag.group(1) if m_tag else tag_str
                
                # Generate missing intermediate landing slab inside the shaft
                try:
                    t_ft = mm_to_ft(200.0)
                    w_landing_ft = mm_to_ft(rd.get('width_mm', 1500))
                    f1_start_y_ft = mm_to_ft(rd['flight_1']['start'][1])
                    
                    std_y_start_ft = f1_start_y_ft - w_landing_ft
                    std_y_end_ft = f1_start_y_ft
                    
                    x_left_ft = mm_to_ft(rd['main_landing']['x_left']) + t_ft/2.0
                    x_right_ft = mm_to_ft(rd['main_landing']['x_right']) - t_ft/2.0
                    
                    c1 = DB.XYZ(x_left_ft, std_y_start_ft, mid_elev)
                    c2 = DB.XYZ(x_right_ft, std_y_start_ft, mid_elev)
                    c3 = DB.XYZ(x_right_ft, std_y_end_ft, mid_elev)
                    c4 = DB.XYZ(x_left_ft, std_y_end_ft, mid_elev)
                    loop = DB.CurveLoop()
                    loop.Append(DB.Line.CreateBound(c1, c2))
                    loop.Append(DB.Line.CreateBound(c2, c3))
                    loop.Append(DB.Line.CreateBound(c3, c4))
                    loop.Append(DB.Line.CreateBound(c4, c1))
                    
                    loops = Generic.List[DB.CurveLoop]()
                    loops.Add(loop)
                    
                    f_type = DB.FilteredElementCollector(doc).OfClass(DB.FloorType).FirstElement()
                    floor = DB.Floor.Create(doc, loops, f_type.Id, mid_lvl.Id)
                    from revit_mcp.utils import safe_set_comment
                    safe_set_comment(floor, "AI_{}_MS_MidLanding".format(c_tag))
                except Exception as e:
                    self.log("Failed to create intermediate landing: {}".format(e))
        t_mid.Commit()
        t_mid.Dispose()
        if intermediate_levels:
            self.log("Stair runs: created {} intermediate levels.".format(
                len(intermediate_levels)))

        # --- Group run_data by core ---
        cores = {}  # core_tag -> [run_data_dicts]
        for rd in run_data_list:
            m = re.match(r'AI_(Stair_\d+)_L\d+_Run', rd['tag'])
            core_tag = m.group(1) if m else "Stair_1"
            cores.setdefault(core_tag, []).append(rd)

        all_stair_ids = []
        from System.Collections.Generic import HashSet  # type: ignore
        existing_fps = getattr(self, '_stair_fps_cache', {})

        for core_tag, core_runs in sorted(cores.items()):
            # â"€â"€ ENRICH AND GROUP BY HEIGHT â"€â"€
            # We must enrich ALL runs with level references before grouping.
            target_runs = []
            for rd in core_runs:
                # Differential Sync Protection: Preserve valid existing stairs
                fp_key = "{}|{}".format(rd['tag'], rd.get('fingerprint',''))
                if rd['tag'] in existing_fps and existing_fps[rd['tag']] == rd.get('fingerprint',''):
                    stair_fec = DB.FilteredElementCollector(doc).OfClass(DB.Architecture.Stairs).WhereElementIsNotElementType()
                    for s in stair_fec:
                        sp = s.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                        if sp and sp.AsString() == fp_key:
                            all_stair_ids.append((s.Id, core_tag))
                            break
                    continue

                bi = rd['base_level_idx']
                ti = rd['top_level_idx']
                if bi < len(current_levels) and ti < len(current_levels):
                    rd['_pair_base_level'] = current_levels[bi]
                    rd['_pair_top_level'] = current_levels[ti]
                    target_runs.append(rd)

            # Unified Batching: Group by CONTIGUOUS blocks of the same height
            # (MultistoryStairs cannot reliably skip levels of different heights)
            contiguous_groups = [] # list of [run_data_dicts]
            if target_runs:
                current_group = [target_runs[0]]
                for i in range(1, len(target_runs)):
                    prev = target_runs[i-1]
                    curr = target_runs[i]
                    h_prev = round(prev['top_elev'] - prev['base_elev'], 1)
                    h_curr = round(curr['top_elev'] - curr['base_elev'], 1)
                    # Must be same height AND contiguous level indices
                    if h_curr == h_prev and curr['base_level_idx'] == prev['top_level_idx']:
                        current_group.append(curr)
                    else:
                        contiguous_groups.append(current_group)
                        current_group = [curr]
                contiguous_groups.append(current_group)

            skipped = []
            for h_idx, seg_runs in enumerate(contiguous_groups):
                # seg_runs now contains a block of contiguous floors of the same height
                if not seg_runs: continue
                ref_rd = seg_runs[0]
                h_val = round(ref_rd['top_elev'] - ref_rd['base_elev'], 1)
                ref_base = ref_rd['_pair_base_level']
                ref_top = ref_rd['_pair_top_level']

                # Create reference stair
                scope = None
                t = None
                ref_stair_id = None
                try:
                    scope = _StairsEditScope(doc, "AI Stair Ref")
                    ref_stair_id = scope.Start(ref_base.Id, ref_top.Id)
                    t = DB.Transaction(doc, "AI Stair Ref Dogleg")
                    t.Start()
                    if ref_rd.get('_intermediate_count', 0) > 0:
                        # Tall floor multi-pair build
                        self._build_multipair_in_scope(
                            doc, ref_stair_id, ref_rd, ref_base, current_levels,
                            tread_ft, hw, landing_ft, spec_riser_mm, spec_tread_mm,
                            _StairsRun, _StairsLanding, _StairsRunJust)
                    else:
                        # Standard typical build
                        self._build_dogleg_in_scope(
                            doc, ref_stair_id, ref_rd, ref_base, current_levels,
                            tread_ft, hw, landing_ft, spec_riser_mm, spec_tread_mm,
                            _StairsRun, _StairsLanding, _StairsRunJust)
                    t.Commit()
                    t.Dispose()
                    t = None
                    scope.Commit(NuclearJoinGuard(doc))
                    scope.Dispose()
                    scope = None
                except Exception as e:
                    self.log("  Core {} h_idx {} ref FAILED: {}".format(core_tag, h_idx, e))
                    if t:
                        try: t.RollBack()
                        except: pass
                        try: t.Dispose()
                        except: pass
                        t = None
                    if scope:
                        try: scope.RollBack()
                        except: pass
                        try: scope.Dispose()
                        except: pass
                        scope = None
                    skipped.extend(seg_runs)
                    continue

                # Clone to other floors in segment
                if len(seg_runs) > 1:
                    ref_stair_el = doc.GetElement(ref_stair_id)
                    if ref_stair_el:
                        try:
                            t_ms = DB.Transaction(doc, "AI MSS {} h_idx {}".format(core_tag, h_idx))
                            t_ms.Start()
                            setup_failure_handling(t_ms, use_nuclear=True)
                            ms = _MSS.Create(ref_stair_el)
                            level_ids = HashSet[DB.ElementId]()
                            for rd in seg_runs:
                                if rd is ref_rd:
                                    continue
                                lvl_id = current_levels[rd['top_level_idx']].Id
                                if ms.CanConnectLevel(lvl_id):
                                    level_ids.Add(lvl_id)
                                else:
                                    skipped.append(rd)
                            if level_ids.Count > 0:
                                ms.ConnectLevels(level_ids)
                            t_ms.Commit()
                            t_ms.Dispose()

                            # Collect stair IDs
                            ms_elem = None
                            try:
                                ref_el = doc.GetElement(ref_stair_id)
                                if ref_el:
                                    ms_id = ref_el.MultistoryStairsId
                                    if ms_id and ms_id != DB.ElementId.InvalidElementId:
                                        ms_elem = doc.GetElement(ms_id)
                            except: pass
                            for rd in seg_runs:
                                lvl_id = current_levels[rd['base_level_idx']].Id
                                found = False
                                if ms_elem:
                                    try:
                                        stair_on = ms_elem.GetStairsOnLevel(lvl_id)
                                        if stair_on:
                                            all_stair_ids.append((stair_on.Id, core_tag))
                                            found = True
                                    except: pass
                                if not found and rd is not ref_rd:
                                    skipped.append(rd)
                            if not found and rd is ref_rd:
                                 all_stair_ids.append((ref_stair_id, core_tag))

                            self.log("  Core {} h_idx {}: cloned to {} floors.".format(
                                core_tag, h_idx, level_ids.Count))
                        except Exception as e:
                            self.log("  Core {} MSS h_idx {} FAILED: {}".format(core_tag, h_idx, e))
                            try: t_ms.RollBack()
                            except: pass
                            skipped.extend([r for r in seg_runs if r is not ref_rd])
                            # Add the prototype even if cloning failed
                            all_stair_ids.append((ref_stair_id, core_tag))
                else:
                    all_stair_ids.append((ref_stair_id, core_tag))

            # (Redundant loops removed as Unified Batching handles them above)
            pass

        # --- Post-config: set widths and comments ---
        if all_stair_ids:
            t_cfg = None
            try:
                t_cfg = DB.Transaction(doc, "AI Stair Config")
                t_cfg.Start()
                setup_failure_handling(t_cfg, use_nuclear=True)
                for stair_id, core_tag in all_stair_ids:
                    try:
                        eid = stair_id if isinstance(stair_id, DB.ElementId) else stair_id.Id
                        stair_el = doc.GetElement(eid)
                        if stair_el:
                            # Differential Sync: Store tag + fingerprint for next-run comparison
                            c_val = "AI_{}_MS".format(core_tag)
                            for rd in run_data_list:
                                # Match this element back to its manifest data
                                if rd['tag'].startswith("AI_{}".format(core_tag)):
                                    # Since we use elements collection, we might need a better match.
                                    # For individual runs, tag is unique.
                                    c_val = "{}|{}".format(rd['tag'], rd.get('fingerprint', ''))
                                    break
                            
                            safe_set_comment(stair_el, c_val)
                            
                            runs_collection = stair_el.GetStairsRuns()
                            # Clean up all auto-railings per user request
                            dep_ids = stair_el.GetDependentElements(DB.ElementCategoryFilter(DB.BuiltInCategory.OST_StairsRailing))
                            for rid in dep_ids:
                                try: doc.Delete(rid)
                                except: pass
                            
                            for sub_id in runs_collection:
                                sub_run = doc.GetElement(sub_id)
                                if sub_run: sub_run.ActualRunWidth = width_ft
                        results["elements"].append(str(eid.Value))
                    except: pass
                t_cfg.Commit()
            except Exception as e:
                self.log("Stair post-config failed: {}".format(e))
                if t_cfg:
                    try: t_cfg.RollBack()
                    except: pass

        # Final diagnostic log dumping riser measurements
        try:
            from Autodesk.Revit.DB import BuiltInParameter as BIP2 # type: ignore
            for sid, c_tag in all_stair_ids:
                s_el = doc.GetElement(sid)
                if s_el:
                    try:
                        act_r = s_el.get_Parameter(BIP2.STAIRS_ACTUAL_NUMBER_OF_RISERS).AsInteger()
                        act_h = ft_to_mm(s_el.get_Parameter(BIP2.STAIRS_ACTUAL_RISER_HEIGHT).AsDouble())
                        self.log("[Stair Diagnostic] ID={} | Risers={} | RiserH={}mm".format(sid, act_r, round(act_h,1)))
                    except Exception: pass
        except Exception: pass

        t_end = time.time()
        self.log("Stair runs: {} cores, {} stairs, {:.1f}s".format(
            len(cores), len(all_stair_ids), t_end - t_start))
        
        if self.tracker:
            self.tracker.record_created("stair_runs", len(all_stair_ids))
            
        return results

    # â"€â"€ Legacy stair methods removed â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€
    # The following were replaced by the single _create_stair_runs above.
    # Stubs kept for compatibility if any external code references them.

    def _create_stair_runs_multistorey(self, current_levels, results):
        """Deprecated -- redirects to _create_stair_runs."""
        self._create_stair_runs(current_levels, results)

    def _create_stair_runs_batched(self, current_levels, results):
        """Deprecated -- redirects to _create_stair_runs."""
        self._create_stair_runs(current_levels, results)

    def _create_stair_runs_legacy(self, current_levels, results):
        """Deprecated -- redirects to _create_stair_runs."""
        self._create_stair_runs(current_levels, results)

    def _create_stair_runs_legacy_for_core(self, *args, **kwargs):
        """Deprecated -- no longer used."""
        pass

    # â"€â"€ End of stair run creation methods â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€â"€

    def _REMOVED_create_stair_runs_multistorey(self, current_levels, results):
        """REMOVED: Old multistorey method kept as dead code reference.

        Uses Revit's MultistoryStairs API:
          1. Group run_data by core, then by floor height
          2. For the most common floor height, create ONE reference stair (StairsEditScope)
          3. Call MultistoryStairs.Create(stairs) + ConnectLevels() to clone
          4. Handle non-standard floor heights as individual stairs

        This is the fastest approach (~10-20s for 5 cores Ã— 50 floors).
        """
        import Autodesk.Revit.DB as DB  # type: ignore
        from Autodesk.Revit.DB.Architecture import MultistoryStairs as _MSS  # type: ignore
        from revit_mcp.utils import setup_failure_handling
        import math
        import time
        import re
        doc = self.doc

        run_data_list = getattr(self, '_stair_run_data', [])
        if not run_data_list:
            self.log("Stair runs (multistorey): no run data, skipping.")
            return

        try:
            _StairsEditScope = DB.StairsEditScope
            from Autodesk.Revit.DB.Architecture import (  # type: ignore
                StairsRun as _StairsRun,
                StairsLanding as _StairsLanding,
                StairsRunJustification as _StairsRunJust)
        except Exception as e:
            self.log("Stair runs (multistorey): CANNOT load Stairs API: {}".format(e))
            raise

        from revit_mcp.preprocessors import NuclearJoinGuard

        # --- Batch delete ALL existing AI stair elements ---
        deleted_count = 0
        ids_to_delete = []
        try:
            for el in DB.FilteredElementCollector(doc).OfCategory(
                    DB.BuiltInCategory.OST_Stairs).WhereElementIsNotElementType().ToElements():
                p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if not p:
                    p = el.LookupParameter("Comments")
                if p and p.HasValue:
                    cmt = p.AsString()
                    if cmt and cmt.startswith("AI_") and "Stair_" in cmt:
                        ids_to_delete.append(el.Id)
            # Also delete any existing MultistoryStairs elements
            try:
                for el in DB.FilteredElementCollector(doc).OfClass(
                        _MSS).ToElements():
                    ids_to_delete.append(el.Id)
            except Exception:
                pass
        except Exception:
            pass

        if ids_to_delete:
            t_del = DB.Transaction(doc, "AI Stair MS Delete")
            t_del.Start()
            setup_failure_handling(t_del, use_nuclear=True)
            for eid in ids_to_delete:
                try:
                    doc.Delete(eid)
                    deleted_count += 1
                except Exception:
                    pass
            t_del.Commit()
            t_del.Dispose()
            self.log("Stair runs (multistorey): deleted {} old elements.".format(deleted_count))

        spec = getattr(self, '_stair_spec', {})
        width_mm = spec.get("width_of_flight", 1500)
        landing_mm = spec.get("landing_width", 1500)
        spec_tread_mm = spec.get("tread", 300)
        spec_riser_mm = spec.get("riser", 150)

        # Pre-configure stair type
        try:
            stair_types = DB.FilteredElementCollector(doc).OfClass(
                DB.Architecture.StairsType).ToElements()
            if not stair_types:
                stair_types = DB.FilteredElementCollector(doc).OfCategory(
                    DB.BuiltInCategory.OST_Stairs).OfClass(DB.ElementType).ToElements()
            if stair_types:
                t_pre = DB.Transaction(doc, "AI Stair Type Pre-Config MS")
                t_pre.Start()
                for st in stair_types:
                    try:
                        p_riser = st.get_Parameter(
                            DB.BuiltInParameter.STAIRS_ATTR_MAX_RISER_HEIGHT)
                        if p_riser and not p_riser.IsReadOnly:
                            p_riser.Set(mm_to_ft(spec_riser_mm))
                        for tp in st.GetOrderedParameters():
                            n = tp.Definition.Name.lower()
                            if 'tread' in n and 'depth' in n and not tp.IsReadOnly:
                                tp.Set(mm_to_ft(spec_tread_mm))
                                break
                    except Exception:
                        pass
                t_pre.Commit()
                t_pre.Dispose()
        except Exception:
            pass

        # --- Group run_data by core ---
        cores = {}
        for rd in run_data_list:
            tag = rd['tag']
            m = re.match(r'AI_(Stair_\d+)_L\d+_Run', tag)
            core_tag = m.group(1) if m else tag
            cores.setdefault(core_tag, []).append(rd)
        for core_tag in cores:
            cores[core_tag].sort(key=lambda r: r['base_level_idx'])

        self.log("Stair runs (multistorey): {} cores, {} total runs.".format(
            len(cores), len(run_data_list)))

        t_start = time.time()
        created_count = 0
        all_stair_ids = []  # (stair_id, core_tag) for post-config

        tread_ft = mm_to_ft(spec_tread_mm)
        hw = mm_to_ft(width_mm) / 2.0
        landing_ft = mm_to_ft(landing_mm)

        for core_tag, core_runs in cores.items():
            # --- Group floors by height for this core ---
            height_groups = {}  # rounded_height_mm -> [run_data, ...]
            for rd in core_runs:
                bi = rd['base_level_idx']
                ti = rd['top_level_idx']
                if bi >= len(current_levels) or ti >= len(current_levels):
                    continue
                h_ft = current_levels[ti].ProjectElevation - current_levels[bi].ProjectElevation
                if h_ft <= 0:
                    continue
                h_mm = round(h_ft * 304.8)
                height_groups.setdefault(h_mm, []).append(rd)

            if not height_groups:
                continue

            # Find the most common floor height (typical)
            typical_h_mm = max(height_groups, key=lambda h: len(height_groups[h]))
            typical_runs = height_groups[typical_h_mm]
            non_typical_runs = []
            for h, runs in height_groups.items():
                if h != typical_h_mm:
                    non_typical_runs.extend(runs)

            self.log("  Core {}: {} typical floors ({}mm), {} non-typical floors.".format(
                core_tag, len(typical_runs), typical_h_mm, len(non_typical_runs)))

            # --- Step 1: Create ONE reference stair for typical height ---
            ref_rd = typical_runs[0]  # Use first typical floor as reference
            ref_base_idx = ref_rd['base_level_idx']
            ref_top_idx = ref_rd['top_level_idx']
            ref_base_lvl = current_levels[ref_base_idx]
            ref_top_lvl = current_levels[ref_top_idx]

            scope = None
            t = None
            ref_stair_id = None
            try:
                scope = _StairsEditScope(doc, "AI Stair MS Ref")
                ref_stair_id = scope.Start(ref_base_lvl.Id, ref_top_lvl.Id)

                t = DB.Transaction(doc, "AI Stair MS Ref Dogleg")
                t.Start()
                setup_failure_handling(t)

                # Build the reference dogleg
                self._build_dogleg_in_scope(
                    doc, ref_stair_id, ref_rd, ref_base_lvl, current_levels,
                    tread_ft, hw, landing_ft, spec_riser_mm, spec_tread_mm,
                    _StairsRun, _StairsLanding, _StairsRunJust)

                t.Commit()
                t = None
                scope.Commit(NuclearJoinGuard(doc))
                scope.Dispose() # Explicit disposal to release Revit lock
                scope = None

                self.log("  Core {}: reference stair created (L{}-L{}).".format(
                    core_tag, ref_base_idx + 1, ref_top_idx + 1))

            except Exception as e:
                self.log("  Core {} reference stair FAILED: {}".format(core_tag, e))
                if t:
                    try: t.RollBack()
                    except: pass
                if scope:
                    try: 
                        scope.RollBack()
                        scope.Dispose()
                    except: pass
                raise  # Let the cascade handle it

            # --- Step 2: MultistoryStairs.Create + ConnectLevels ---
            ref_stair_el = doc.GetElement(ref_stair_id)
            if not ref_stair_el:
                self.log("  Core {}: could not retrieve reference stair element.".format(core_tag))
                continue

            skipped_typical_runs = []
            try:
                t_ms = DB.Transaction(doc, "AI MultistoryStairs {}".format(core_tag))
                t_ms.Start()
                setup_failure_handling(t_ms, use_nuclear=True)

                ms = _MSS.Create(ref_stair_el)

                # Collect level IDs for all OTHER typical floors (skip the reference)
                from System.Collections.Generic import HashSet  # type: ignore
                level_ids_to_connect = HashSet[DB.ElementId]()
                for rd in typical_runs:
                    if rd is ref_rd:
                        continue
                    bi = rd['base_level_idx']
                    if bi >= len(current_levels):
                        continue
                    lvl_id = current_levels[bi].Id
                    if ms.CanConnectLevel(lvl_id):
                        level_ids_to_connect.Add(lvl_id)
                    else:
                        skipped_typical_runs.append(rd)
                        self.log("  Core {}: L{} cannot be connected (CanConnectLevel=false), "
                                 "will create individually.".format(core_tag, bi + 1))

                if level_ids_to_connect.Count > 0:
                    ms.ConnectLevels(level_ids_to_connect)
                    self.log("  Core {}: ConnectLevels OK -- {} typical floors cloned.".format(
                        core_tag, level_ids_to_connect.Count))

                t_ms.Commit()
                t_ms.Dispose()

                # Retrieve the MultistoryStairs element via the reference stair
                ms_elem = None
                try:
                    ref_el = doc.GetElement(ref_stair_id)
                    if ref_el:
                        ms_id = ref_el.MultistoryStairsId
                        if ms_id and ms_id != DB.ElementId.InvalidElementId:
                            ms_elem = doc.GetElement(ms_id)
                except Exception:
                    pass

                # Collect individual stair IDs using GetStairsOnLevel.
                # GetAllStairsIds() only returns one per height-group (prototype),
                # NOT one per connected level.  GetStairsOnLevel returns the
                # actual stair ElementId at each base level.
                # Also verify each expected stair was created -- any missing
                # ones get added to the individual-creation list.
                expected_runs = [r for r in typical_runs if r not in skipped_typical_runs]
                for rd in expected_runs:
                    bi = rd['base_level_idx']
                    if bi >= len(current_levels):
                        continue
                    lvl_id = current_levels[bi].Id
                    found = False
                    if ms_elem:
                        try:
                            stair_on_level = ms_elem.GetStairsOnLevel(lvl_id)
                            # GetStairsOnLevel returns a Stairs element (not ElementId)
                            if stair_on_level:
                                all_stair_ids.append((stair_on_level.Id, core_tag))
                                found = True
                        except Exception:
                            pass
                    if not found:
                        skipped_typical_runs.append(rd)
                        self.log("  Core {}: L{} stair missing after ConnectLevels, "
                                 "will create individually.".format(core_tag, bi + 1))

                self.log("  Core {}: {} stairs found in MultistoryStairs, {} need individual creation.".format(
                    core_tag, len(all_stair_ids), len(skipped_typical_runs)))
                created_count += 1

            except Exception as e:
                self.log("  Core {} MultistoryStairs.Create/ConnectLevels FAILED: {}".format(
                    core_tag, e))
                try: t_ms.RollBack()
                except: pass
                raise

            # --- Step 3: Handle non-typical + unconnectable floors individually ---
            all_individual_runs = list(non_typical_runs) + skipped_typical_runs
            for rd in all_individual_runs:
                bi = rd['base_level_idx']
                ti = rd['top_level_idx']
                if bi >= len(current_levels) or ti >= len(current_levels):
                    continue
                b_lvl = current_levels[bi]
                t_lvl = current_levels[ti]

                scope2 = None
                t2 = None
                try:
                    scope2 = _StairsEditScope(doc, "AI Stair MS NonTyp")
                    nt_stair_id = scope2.Start(b_lvl.Id, t_lvl.Id)

                    t2 = DB.Transaction(doc, "AI Stair MS NonTyp Dogleg")
                    t2.Start()
                    setup_failure_handling(t2)

                    self._build_dogleg_in_scope(
                        doc, nt_stair_id, rd, b_lvl, current_levels,
                        tread_ft, hw, landing_ft, spec_riser_mm, spec_tread_mm,
                        _StairsRun, _StairsLanding, _StairsRunJust)

                    t2.Commit()
                    t2 = None
                    scope2.Commit(NuclearJoinGuard(doc))
                    scope2 = None

                    all_stair_ids.append((nt_stair_id, core_tag))
                    self.log("  Core {}: non-typical stair L{}-L{} created.".format(
                        core_tag, bi + 1, ti + 1))

                except Exception as e:
                    self.log("  Core {} non-typical L{} FAILED: {}".format(core_tag, bi + 1, e))
                finally:
                    if t2:
                        try: t2.RollBack()
                        except: pass
                    if scope2:
                        try: 
                            scope2.RollBack()
                            scope2.Dispose()
                        except: pass

        # --- Batch post-config ---
        if all_stair_ids:
            t_cfg = None
            try:
                t_cfg = DB.Transaction(doc, "AI Stair MS Config")
                t_cfg.Start()
                setup_failure_handling(t_cfg, use_nuclear=True)
                width_ft = mm_to_ft(width_mm)
                for stair_id, core_tag in all_stair_ids:
                    try:
                        eid = stair_id if isinstance(stair_id, DB.ElementId) else stair_id.Id
                        stair_el = doc.GetElement(eid)
                        if stair_el:
                            safe_set_comment(stair_el, "AI_{}_MS".format(core_tag))
                            
                            runs_collection = stair_el.GetStairsRuns()
                            
                            # Clean up all auto-railings per user request
                            dep_ids = stair_el.GetDependentElements(DB.ElementCategoryFilter(DB.BuiltInCategory.OST_StairsRailing))
                            for rid in dep_ids:
                                try:
                                    doc.Delete(rid)
                                except Exception:
                                    pass
                            
                            for sub_id in runs_collection:
                                sub_run = doc.GetElement(sub_id)
                                if sub_run:
                                    sub_run.ActualRunWidth = width_ft
                        results["elements"].append(str(eid.Value))
                    except Exception:
                        pass
                t_cfg.Commit()
            except Exception as cfg_err:
                self.log("Stair post-config failed: {}".format(cfg_err))
                if t_cfg:
                    try: t_cfg.RollBack()
                    except: pass
            finally:
                if t_cfg:
                    try: t_cfg.Dispose()
                    except: pass

        elapsed = time.time() - t_start
        self.log("Stair runs: {} stairs total. Time: {:.1f}s".format(len(all_stair_ids), elapsed))
        if self.tracker: self.tracker.record_created("stair_runs", len(all_stair_ids))

    def _build_dogleg_in_scope(self, doc, stair_id, rd, base_lvl, current_levels,
                                tread_ft, hw, landing_ft, spec_riser_mm, spec_tread_mm,
                                _StairsRun, _StairsLanding, _StairsRunJust):
        """Build a single dogleg stair inside an already-open StairsEditScope.

        Creates ONE dogleg: Run A (+Y left side), mid-landing (U-turn),
        Run B (-Y right side). Used by typical single-pair floors.

        Mid-landing elevation is computed EXPLICITLY as a_risers * riser_h_ft.
        We do NOT use run_a.TopElevation because Revit adds a nosing offset
        that makes it 1 riser height taller than the actual rise of the run.
        """
        import Autodesk.Revit.DB as DB  # type: ignore

        base_z = base_lvl.ProjectElevation

        f1 = rd['flight_1']
        f2 = rd['flight_2']
        f1_cx = mm_to_ft(f1['start'][0])
        f2_cx = mm_to_ft(f2['start'][0])
        flight_y_start_ft = mm_to_ft(f1['start'][1])
        # Detect 180° rotation: after rotation f1 goes -Y. Use min/max for left/right.
        f1_y_end = mm_to_ft(f1['end'][1])
        is_rotated = (f1_y_end < flight_y_start_ft)
        left_cx  = min(f1_cx, f2_cx)
        right_cx = max(f1_cx, f2_cx)
        land_x_left  = left_cx  - hw
        land_x_right = right_cx + hw

        flight_list = rd.get('flight_list', [])
        # --- Asymmetrical Riser Split Fix (Problem 1) ---
        # If flight_list is strictly determined in manifest (e.g. 14/16 split), use it.
        if len(flight_list) >= 2:
            a_risers = flight_list[0]
            b_risers = flight_list[1]
        elif 'actual_risers_a' in rd:
            a_risers = rd['actual_risers_a']
            b_risers = rd.get('actual_risers_b', a_risers)
        elif len(flight_list) == 1:
            a_risers = flight_list[0]
            b_risers = 0
        else:
            a_risers = rd.get('risers_per_flight', 14)
            b_risers = a_risers

        total_risers = a_risers + b_risers

        # Compute actual riser height: scope height / total risers
        try:
            top_lvl = rd.get('_pair_top_level') or current_levels[rd['top_level_idx']]
            scope_height_ft = round(top_lvl.ProjectElevation - base_z, 8)
        except Exception:
            scope_height_ft = round(mm_to_ft(spec_riser_mm * total_risers), 8)
        riser_h_ft = (scope_height_ft / total_risers) if total_risers > 0 else mm_to_ft(spec_riser_mm)

        # Set DesiredNumberRisers on stair element to hint to Revit
        try:
            stair_el = doc.GetElement(stair_id)
            if stair_el:
                from Autodesk.Revit.DB import BuiltInParameter as BIP2 # type: ignore
                p_desired = stair_el.get_Parameter(BIP2.STAIRS_DESIRED_NUMBER_OF_RISERS)
                if p_desired and not p_desired.IsReadOnly:
                    p_desired.Set(total_risers)
        except Exception: pass

        # --- Run A: direction depends on rotation ---
        a_run_len = max(a_risers - 1, 1) * tread_ft
        p_as = DB.XYZ(round(f1_cx, 8), round(flight_y_start_ft, 8), base_z)
        if is_rotated:
            p_ae = DB.XYZ(round(f1_cx, 8), round(flight_y_start_ft - a_run_len, 8), base_z)
        else:
            p_ae = DB.XYZ(round(f1_cx, 8), round(flight_y_start_ft + a_run_len, 8), base_z)

        # Diagnostic Log
        if a_run_len < 0.001:
            self.log("  [Stair DIAGNOSTIC] {} - Run A length too short! a_risers={}, total_risers={}, total_h={:.4f}ft".format(
                rd['tag'], a_risers, total_risers, scope_height_ft))

        try:
            _StairsRun.CreateStraightRun(doc, stair_id, DB.Line.CreateBound(p_as, p_ae), _StairsRunJust.Center)
        except Exception as e:
            self.log("  [Stair DIAGNOSTIC FAILED] {} Run A: p_as={}, p_ae={}, error={}".format(
                rd['tag'], p_as, p_ae, e))
            raise e

        mid_elev_rel = a_risers * riser_h_ft
        mid_elev_abs = base_z + mid_elev_rel

        if b_risers <= 0:
            return

        if "dyn_landing_w_mm" in rd:
            dyn_landing_ft = mm_to_ft(rd["dyn_landing_w_mm"])
        else:
            dyn_landing_ft = landing_ft

        # --- Mid-landing (U-turn at far side of Run A) ---
        if is_rotated:
            land_y_top = round(flight_y_start_ft - a_run_len, 8)
            land_y_bot = round(land_y_top - dyn_landing_ft, 8)
        else:
            land_y_bot = round(flight_y_start_ft + a_run_len, 8)
            land_y_top = round(land_y_bot + dyn_landing_ft, 8)

        lp1 = DB.XYZ(round(land_x_left,  8), land_y_bot, mid_elev_abs)
        lp2 = DB.XYZ(round(land_x_right, 8), land_y_bot, mid_elev_abs)
        lp3 = DB.XYZ(round(land_x_right, 8), land_y_top, mid_elev_abs)
        lp4 = DB.XYZ(round(land_x_left,  8), land_y_top, mid_elev_abs)

        mid_loop = DB.CurveLoop()
        mid_loop.Append(DB.Line.CreateBound(lp1, lp2))
        mid_loop.Append(DB.Line.CreateBound(lp2, lp3))
        mid_loop.Append(DB.Line.CreateBound(lp3, lp4))
        mid_loop.Append(DB.Line.CreateBound(lp4, lp1))
        _StairsLanding.CreateSketchedLanding(doc, stair_id, mid_loop, mid_elev_rel)

        # --- Run B: returns toward starting side ---
        b_run_len = max(b_risers - 1, 1) * tread_ft
        if is_rotated:
            # Mid-landing is at lower Y; Run B goes back toward +Y (start side)
            p_bs = DB.XYZ(round(f2_cx, 8), round(flight_y_start_ft - a_run_len, 8), mid_elev_abs)
            p_be = DB.XYZ(round(f2_cx, 8), round(flight_y_start_ft - a_run_len + b_run_len, 8), mid_elev_abs)
        else:
            # Mid-landing is at higher Y; Run B goes back toward -Y (start side)
            p_bs = DB.XYZ(round(f2_cx, 8), land_y_bot, mid_elev_abs)
            p_be = DB.XYZ(round(f2_cx, 8), round(land_y_bot - b_run_len, 8), mid_elev_abs)
        
        # Diagnostic Log
        if b_run_len < 0.001:
            self.log("  [Stair DIAGNOSTIC] {} - Run B length too short! b_risers={}".format(rd['tag'], b_risers))

        try:
            line_b = DB.Line.CreateBound(p_bs, p_be)
            run_b = _StairsRun.CreateStraightRun(doc, stair_id, line_b, _StairsRunJust.Center)
        except Exception as e:
            self.log("  [Stair DIAGNOSTIC FAILED] {} Run B: p_bs={}, p_be={}, error={}".format(
                rd['tag'], p_bs, p_be, e))
            raise e
        
        try:
            p_nr_b = run_b.LookupParameter("Number of Risers") or run_b.LookupParameter("Actual Number of Risers")
            if p_nr_b and not p_nr_b.IsReadOnly: p_nr_b.Set(b_risers)
        except Exception: pass

        # Detailed logging -- what WE asked for AND what Revit actually set
        try:
            flight_w_mm = hw * 2.0 * 304.8
            tread_mm = tread_ft * 304.8
            run_a_len_mm = a_run_len * 304.8
            run_b_len_mm = b_run_len * 304.8
            mid_elev_mm = mid_elev_rel * 304.8
            
            # Read back what Revit actually stored on the stair element
            actual_risers_revit = "?"
            actual_riser_h_revit = "?"
            actual_tread_revit = "?"
            desired_risers_readonly = "?"
            try:
                from Autodesk.Revit.DB import BuiltInParameter as BIP2 # type: ignore
                se = doc.GetElement(stair_id)
                if se:
                    p_act_r = se.get_Parameter(BIP2.STAIRS_RUN_ACTUAL_NUMBER_OF_RISERS)
                    if p_act_r: actual_risers_revit = p_act_r.AsInteger()
                    p_act_rh = se.get_Parameter(BIP2.STAIRS_RUN_ACTUAL_RISER_HEIGHT)
                    if p_act_rh: actual_riser_h_revit = "{:.1f}mm".format(p_act_rh.AsDouble() * 304.8)
                    p_act_t = se.get_Parameter(BIP2.STAIRS_RUN_ACTUAL_TREAD_DEPTH)
                    if p_act_t: actual_tread_revit = "{:.1f}mm".format(p_act_t.AsDouble() * 304.8)
                    p_des = se.get_Parameter(BIP2.STAIRS_DESIRED_NUMBER_OF_RISERS)
                    if p_des: desired_risers_readonly = "RO={}".format(p_des.IsReadOnly)
            except Exception as rb_e:
                actual_risers_revit = "err:{}".format(rb_e)

            log_str = (
                "    [Stair Diagnostic] Dogleg ID={id} | Z_Start={z:.0f}mm | MidElev={me:.0f}mm | "
                "ASKED: A={ra}r+B={rb}r={tr}total Tread={trd:.0f}mm RunW={w:.0f}mm | "
                "REVIT_ACTUAL: risers={ar} riserH={arh} tread={at} DesiredRO={dro}"
            ).format(
                id=stair_id.Value if hasattr(stair_id, "Value") else str(stair_id),
                z=base_lvl.ProjectElevation * 304.8, me=mid_elev_mm,
                ra=a_risers, rb=b_risers, tr=a_risers + b_risers,
                trd=tread_mm, w=flight_w_mm,
                ar=actual_risers_revit, arh=actual_riser_h_revit,
                at=actual_tread_revit, dro=desired_risers_readonly
            )
            self.log(log_str)
        except Exception as e:
            self.log("    [Stair Diagnostic] Logging failed: {}".format(e))

    def _create_stair_runs_batched(self, current_levels, results):
        """BATCHED: One StairsEditScope per core spanning all floors.

        Groups run_data by staircase core (e.g. all AI_Stair_1_L*_Run entries
        become one multi-storey stair element).  All runs + landings for all
        floors are added inside a single scope, committed once.
        """
        import Autodesk.Revit.DB as DB  # type: ignore
        from revit_mcp.utils import setup_failure_handling
        import math
        import time
        doc = self.doc

        run_data_list = getattr(self, '_stair_run_data', [])
        if not run_data_list:
            self.log("Stair runs (batched): no run data available, skipping.")
            return

        try:
            _StairsEditScope = DB.StairsEditScope
            from Autodesk.Revit.DB.Architecture import (  # type: ignore
                StairsRun as _StairsRun,
                StairsLanding as _StairsLanding,
                StairsRunJustification as _StairsRunJust)
        except Exception as e:
            self.log("Stair runs (batched): CANNOT load Stairs API: {}".format(e))
            raise

        from revit_mcp.preprocessors import NuclearJoinGuard

        # --- Batch delete ALL existing AI stair elements in ONE transaction ---
        deleted_count = 0
        ids_to_delete = []
        try:
            for el in DB.FilteredElementCollector(doc).OfCategory(
                    DB.BuiltInCategory.OST_Stairs).WhereElementIsNotElementType().ToElements():
                p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if not p:
                    p = el.LookupParameter("Comments")
                if p and p.HasValue:
                    cmt = p.AsString()
                    if cmt and cmt.startswith("AI_") and "Stair_" in cmt:
                        ids_to_delete.append(el.Id)
        except Exception:
            pass

        if ids_to_delete:
            t_del = DB.Transaction(doc, "AI Stair Batch Delete")
            t_del.Start()
            setup_failure_handling(t_del, use_nuclear=True)
            for eid in ids_to_delete:
                try:
                    doc.Delete(eid)
                    deleted_count += 1
                except Exception:
                    pass
            t_del.Commit()
            t_del.Dispose()
            self.log("Stair runs (batched): deleted {} old stair elements in 1 transaction.".format(deleted_count))

        spec = getattr(self, '_stair_spec', {})
        width_mm = spec.get("width_of_flight", 1500)
        landing_mm = spec.get("landing_width", 1500)
        spec_tread_mm = spec.get("tread", 300)
        spec_riser_mm = spec.get("riser", 150)

        # --- Pre-configure stair type ---
        try:
            stair_types = DB.FilteredElementCollector(doc).OfClass(
                DB.Architecture.StairsType).ToElements()
            if not stair_types:
                stair_types = DB.FilteredElementCollector(doc).OfCategory(
                    DB.BuiltInCategory.OST_Stairs).OfClass(DB.ElementType).ToElements()
            if stair_types:
                t_pre = DB.Transaction(doc, "AI Stair Type Pre-Config")
                t_pre.Start()
                for st in stair_types:
                    try:
                        p_riser = st.get_Parameter(
                            DB.BuiltInParameter.STAIRS_ATTR_MAX_RISER_HEIGHT)
                        if p_riser and not p_riser.IsReadOnly:
                            p_riser.Set(mm_to_ft(spec_riser_mm))
                        for tp in st.GetOrderedParameters():
                            n = tp.Definition.Name.lower()
                            if 'tread' in n and 'depth' in n and not tp.IsReadOnly:
                                tp.Set(mm_to_ft(spec_tread_mm))
                                break
                    except Exception:
                        pass
                t_pre.Commit()
                t_pre.Dispose()
        except Exception as e:
            self.log("Stair runs (batched): type pre-config note: {}".format(e))

        # --- Group run_data by staircase core ---
        # Tag format: "AI_Stair_N_LX_Run" -- core identifier is "Stair_N"
        import re
        cores = {}  # core_tag -> [run_data, ...] sorted by base_level_idx
        for rd in run_data_list:
            tag = rd['tag']
            m = re.match(r'AI_(Stair_\d+)_L\d+_Run', tag)
            core_tag = m.group(1) if m else tag
            cores.setdefault(core_tag, []).append(rd)
        # Sort each core's runs by base level
        for core_tag in cores:
            cores[core_tag].sort(key=lambda r: r['base_level_idx'])

        self.log("Stair runs (batched): {} cores, {} total runs.".format(
            len(cores), len(run_data_list)))

        created_count = 0
        created_stair_ids = []  # (stair_id, core_tag, width_mm) for post-config
        t_start = time.time()

        for core_tag, core_runs in cores.items():
            # Find overall base and top levels for this core
            base_idx = core_runs[0]['base_level_idx']
            top_idx = core_runs[-1]['top_level_idx']
            if base_idx >= len(current_levels) or top_idx >= len(current_levels):
                self.log("  Core {}: level index out of range, skipping.".format(core_tag))
                continue

            base_lvl = current_levels[base_idx]
            top_lvl = current_levels[top_idx]

            scope = None
            t = None
            stair_id = None
            succeeded = False
            try:
                scope = _StairsEditScope(doc, "AI Stair Batched")
                stair_id = scope.Start(base_lvl.Id, top_lvl.Id)

                t = DB.Transaction(doc, "AI Stair Dogleg Batched")
                t.Start()
                setup_failure_handling(t)

                # Push spec riser onto stair type
                stair_el = doc.GetElement(stair_id)
                if stair_el:
                    try:
                        stair_type = doc.GetElement(stair_el.GetTypeId())
                        if stair_type:
                            p_riser = stair_type.get_Parameter(
                                DB.BuiltInParameter.STAIRS_ATTR_MAX_RISER_HEIGHT)
                            if p_riser and not p_riser.IsReadOnly:
                                p_riser.Set(mm_to_ft(spec_riser_mm))
                    except Exception:
                        pass

                tread_ft = mm_to_ft(spec_tread_mm)
                hw = mm_to_ft(width_mm) / 2.0
                landing_ft = mm_to_ft(landing_mm)

                # Initial pass: calculate total risers for the ENTIRE core height
                total_core_risers = 0
                for rd in core_runs:
                    rd_base_lvl = current_levels[rd['base_level_idx']]
                    rd_top_lvl = current_levels[rd['top_level_idx']]
                    fh_mm = round((rd_top_lvl.ProjectElevation - rd_base_lvl.ProjectElevation) * 304.8)
                    n_flights = rd.get('num_flight_pairs', 1) * 2
                    f_risers = int(math.ceil(fh_mm / float(spec_riser_mm)))
                    if n_flights > 0 and f_risers % n_flights != 0:
                        f_risers = int(math.ceil(f_risers / float(n_flights))) * n_flights
                    total_core_risers += f_risers

                stair_el = doc.GetElement(stair_id)
                if stair_el:
                    try:
                        p_desired = stair_el.get_Parameter(DB.BuiltInParameter.STAIRS_DESIRED_NUMBER_OF_RISERS)
                        if p_desired and not p_desired.IsReadOnly:
                            p_desired.Set(total_core_risers)
                    except: pass

                # Process each floor's runs within this single scope
                for rd in core_runs:
                    rd_base_idx = rd['base_level_idx']
                    rd_top_idx = rd['top_level_idx']
                    if rd_base_idx >= len(current_levels) or rd_top_idx >= len(current_levels):
                        continue

                    rd_base_lvl = current_levels[rd_base_idx]
                    rd_top_lvl = current_levels[rd_top_idx]
                    floor_h_ft = rd_top_lvl.ProjectElevation - rd_base_lvl.ProjectElevation
                    if floor_h_ft <= 0:
                        continue
                    base_z = rd_base_lvl.ProjectElevation

                    num_pairs = rd.get('num_flight_pairs', 1)
                    f1 = rd['flight_1']
                    f2 = rd['flight_2']
                    f1_cx = mm_to_ft(f1['start'][0])
                    f2_cx = mm_to_ft(f2['start'][0])
                    flight_y_start_ft = mm_to_ft(f1['start'][1])

                    land_x_left = f1_cx - hw
                    land_x_right = f2_cx + hw

                    # Re-calculate riser distribution for THIS floor
                    floor_h_mm = round(floor_h_ft * 304.8)
                    curr_floor_risers = int(math.ceil(floor_h_mm / float(spec_riser_mm)))
                    num_flights = num_pairs * 2
                    if num_flights > 0 and curr_floor_risers % num_flights != 0:
                        curr_floor_risers = int(math.ceil(curr_floor_risers / float(num_flights))) * num_flights

                    rpf = rd.get('risers_per_flight', max(curr_floor_risers // 2, 1))
                    flight_risers_list = [rpf] * num_flights

                    flight_pairs = []
                    for fi in range(0, len(flight_risers_list), 2):
                        a = flight_risers_list[fi]
                        b = flight_risers_list[fi + 1] if fi + 1 < len(flight_risers_list) else 0
                        if a > 0:
                            flight_pairs.append((a, b))

                    self.log("  Stair {}: total_risers={} rpf={} pairs={} "
                             "flights={} floor_h={:.0f}mm".format(
                        rd['tag'], total_risers, rpf, len(flight_pairs),
                        flight_risers_list, floor_h_mm))

                    # Elevation tracking is RELATIVE to the stair's base level
                    # For multi-storey: offset from the overall base
                    storey_base_rel = base_z - base_lvl.ProjectElevation
                    current_elev_rel = storey_base_rel

                    for p_idx, (a_risers, b_risers) in enumerate(flight_pairs):
                        current_elev_abs = base_lvl.ProjectElevation + current_elev_rel

                        # NOTE: No explicit intermediate landing between pairs.
                        # Revit auto-generates landings between consecutive runs.
                        # Manually creating one here caused overlapping sketch
                        # lines ("Highlighted lines overlap" warning + 20s stall).

                        # Run A: left side, going +Y
                        a_run_len = max(a_risers - 1, 1) * tread_ft
                        p_as = DB.XYZ(f1_cx, flight_y_start_ft, current_elev_abs)
                        p_ae = DB.XYZ(f1_cx, flight_y_start_ft + a_run_len,
                                      current_elev_abs)
                        line_a = DB.Line.CreateBound(p_as, p_ae)
                        run_a = _StairsRun.CreateStraightRun(
                            doc, stair_id, line_a, _StairsRunJust.Center)

                        mid_elev_rel = run_a.TopElevation
                        mid_elev_abs = base_lvl.ProjectElevation + mid_elev_rel

                        # Mid-landing (U-turn at back of shaft)
                        land_y_bot = flight_y_start_ft + a_run_len
                        land_y_top = land_y_bot + landing_ft
                        lp1 = DB.XYZ(land_x_left,  land_y_bot, mid_elev_abs)
                        lp2 = DB.XYZ(land_x_right, land_y_bot, mid_elev_abs)
                        lp3 = DB.XYZ(land_x_right, land_y_top, mid_elev_abs)
                        lp4 = DB.XYZ(land_x_left,  land_y_top, mid_elev_abs)
                        mid_loop = DB.CurveLoop()
                        mid_loop.Append(DB.Line.CreateBound(lp1, lp2))
                        mid_loop.Append(DB.Line.CreateBound(lp2, lp3))
                        mid_loop.Append(DB.Line.CreateBound(lp3, lp4))
                        mid_loop.Append(DB.Line.CreateBound(lp4, lp1))
                        _StairsLanding.CreateSketchedLanding(
                            doc, stair_id, mid_loop, mid_elev_rel)

                        # Run B: right side, going -Y
                        if b_risers <= 0:
                            current_elev_rel = mid_elev_rel
                            continue

                        b_run_len = max(b_risers - 1, 1) * tread_ft
                        p_bs = DB.XYZ(f2_cx, flight_y_start_ft + a_run_len,
                                      mid_elev_abs)
                        p_be = DB.XYZ(f2_cx,
                                      flight_y_start_ft + a_run_len - b_run_len,
                                      mid_elev_abs)
                        line_b = DB.Line.CreateBound(p_bs, p_be)
                        run_b = _StairsRun.CreateStraightRun(
                            doc, stair_id, line_b, _StairsRunJust.Center)

                        current_elev_rel = run_b.TopElevation

                # Commit the entire multi-storey stair in one go
                t.Commit()
                t = None
                scope.Commit(NuclearJoinGuard(doc))
                scope = None
                succeeded = True
                created_stair_ids.append((stair_id, core_tag, width_mm))
                created_count += 1
                self.log("  Core {} committed ({} floors in 1 scope).".format(
                    core_tag, len(core_runs)))

            except Exception as e:
                import traceback
                self.log("  Core {} FAILED: {}\n{}".format(
                    core_tag, e, traceback.format_exc()))
            finally:
                if t is not None:
                    try: t.RollBack()
                    except Exception: pass
                    try: t.Dispose()
                    except Exception: pass
                if scope is not None:
                    try: scope.RollBack()
                    except Exception: pass
                    try: scope.Dispose()
                    except Exception: pass

            if not succeeded:
                self.log("  Core {} failed -- will attempt legacy fallback for this core.".format(core_tag))
                # Fall back to legacy per-floor for this specific core
                try:
                    self._create_stair_runs_legacy_for_core(
                        core_tag, core_runs, current_levels, results,
                        _StairsEditScope, _StairsRun, _StairsLanding,
                        _StairsRunJust)
                except Exception as legacy_err:
                    self.log("  Core {} legacy fallback also failed: {}".format(
                        core_tag, legacy_err))

        # --- Batch post-config: tag + apply preset width in ONE transaction ---
        if created_stair_ids:
            t_cfg = DB.Transaction(doc, "AI Stair Batch Config")
            t_cfg.Start()
            setup_failure_handling(t_cfg, use_nuclear=True)
            for stair_id, core_tag, w_mm in created_stair_ids:
                try:
                    stair_el = doc.GetElement(stair_id)
                    if stair_el:
                        safe_set_comment(stair_el, "AI_{}_MultiStorey".format(core_tag))
                        width_ft = mm_to_ft(w_mm)
                        for sub_id in stair_el.GetStairsRuns():
                            sub_run = doc.GetElement(sub_id)
                            if sub_run:
                                sub_run.ActualRunWidth = width_ft
                except Exception:
                    pass
                results["elements"].append(str(stair_id.Value))
            t_cfg.Commit()
            t_cfg.Dispose()

        elapsed = time.time() - t_start
        self.log("Stair runs (batched): {} cores created, {} deleted. Time: {:.1f}s".format(
            created_count, deleted_count, elapsed))
        if self.tracker: self.tracker.record_created("stair_runs", len(created_stair_ids))

    def _create_stair_runs_legacy_for_core(self, core_tag, core_runs, current_levels,
                                            results, _StairsEditScope, _StairsRun,
                                            _StairsLanding, _StairsRunJust):
        """Legacy per-floor stair creation for a single core (used as fallback)."""
        import Autodesk.Revit.DB as DB  # type: ignore
        from revit_mcp.utils import setup_failure_handling
        from revit_mcp.preprocessors import NuclearJoinGuard
        import math
        doc = self.doc

        spec = getattr(self, '_stair_spec', {})
        width_mm = spec.get("width_of_flight", 1500)
        landing_mm = spec.get("landing_width", 1500)
        spec_tread_mm = spec.get("tread", 300)
        spec_riser_mm = spec.get("riser", 150)

        for rd in core_runs:
            tag = rd['tag']
            base_idx = rd['base_level_idx']
            top_idx = rd['top_level_idx']
            if base_idx >= len(current_levels) or top_idx >= len(current_levels):
                continue

            base_lvl = current_levels[base_idx]
            top_lvl = current_levels[top_idx]
            floor_h_ft = top_lvl.ProjectElevation - base_lvl.ProjectElevation
            if floor_h_ft <= 0:
                continue
            base_z = base_lvl.ProjectElevation

            num_pairs = rd.get('num_flight_pairs', 1)
            f1 = rd['flight_1']
            f2 = rd['flight_2']
            hw = mm_to_ft(width_mm) / 2.0
            landing_ft = mm_to_ft(landing_mm)
            tread_ft = mm_to_ft(spec_tread_mm)
            f1_cx = mm_to_ft(f1['start'][0])
            f2_cx = mm_to_ft(f2['start'][0])
            flight_y_start_ft = mm_to_ft(f1['start'][1])
            land_x_left = f1_cx - hw
            land_x_right = f2_cx
            try:
                scope = _StairsEditScope(doc, "AI Stair Legacy")
                stair_id = scope.Start(base_lvl.Id, top_lvl.Id)

                t = DB.Transaction(doc, "AI Stair Dogleg Legacy")
                t.Start()
                setup_failure_handling(t)

                # --- NEW: Use manifestation points directly to support rotation ---
                p1_start = DB.Point(mm_to_ft(f1['start'][0]), mm_to_ft(f1['start'][1]), base_z)
                p1_end   = DB.Point(mm_to_ft(f1['end'][0]),   mm_to_ft(f1['end'][1]),   base_z)
                p2_start = DB.Point(mm_to_ft(f2['start'][0]), mm_to_ft(f2['start'][1]), base_z)
                p2_end   = DB.Point(mm_to_ft(f2['end'][0]),   mm_to_ft(f2['end'][1]),   base_z)

                run1 = _StairsRun.CreateStraightRun(doc, stair_id, base_lvl.Id, 
                        DB.Line.CreateBound(p1_start, p1_end),
                        _StairsRunJust.Center)
                
                run2 = _StairsRun.CreateStraightRun(doc, stair_id, base_lvl.Id, 
                        DB.Line.CreateBound(p2_start, p2_end),
                        _StairsRunJust.Center)

                # --- FORCE PERFECT RISER PARITY (Revit 2026 Fix) ---
                # Any remainder in riser distribution creates a Y-gap between flights. 
                # For 4 flights, we must ensure total_risers is a multiple of 4.
                # For 2 flights, a multiple of 2.
                num_flights = num_pairs * 2
                if num_flights > 0 and total_risers % num_flights != 0:
                    total_risers = int(math.ceil(total_risers / float(num_flights))) * num_flights

                # Distribute risers strictly EQUALLY
                flight_risers_list = []
                if num_flights > 0:
                    per_flight = total_risers // num_flights
                    flight_risers_list = [per_flight] * num_flights
                else:
                    flight_risers_list = [total_risers]

                flight_pairs = []
                for fi in range(0, len(flight_risers_list), 2):
                    a = flight_risers_list[fi]
                    b = flight_risers_list[fi + 1] if fi + 1 < len(flight_risers_list) else 0
                    if a > 0:
                        flight_pairs.append((a, b))

                # Explicitly set the DesiredNumberRisers to match our even count
                stair_el = doc.GetElement(stair_id)
                if stair_el:
                    # Robust parameter lookup: try BuiltIn enum, then string name
                    p_desired = None
                    try: p_desired = stair_el.LookupParameter("Desired Number of Risers")
                    except: pass
                    if not p_desired:
                        try: 
                            from Autodesk.Revit.DB import BuiltInParameter as BIP2 # type: ignore
                            p_desired = stair_el.get_Parameter(BIP2.STAIRS_DESIRED_NUMBER_OF_RISERS)
                        except Exception: pass
                    if p_desired and not p_desired.IsReadOnly:
                        p_desired.Set(total_risers)
                    elif hasattr(stair_el, "DesiredNumberRisers"):
                        try: stair_el.DesiredNumberRisers = total_risers
                        except: pass

                current_elev_rel = 0.0
                for p_idx, (a_risers, b_risers) in enumerate(flight_pairs):
                    current_elev_abs = base_z + current_elev_rel

                    # No explicit intermediate landing -- Revit auto-generates
                    # landings between consecutive runs (avoids overlap warning).

                    a_run_len = max(a_risers - 1, 1) * tread_ft
                    p_as = DB.XYZ(f1_cx, flight_y_start_ft, current_elev_abs)
                    p_ae = DB.XYZ(f1_cx, flight_y_start_ft + a_run_len, current_elev_abs)
                    line_a = DB.Line.CreateBound(p_as, p_ae)
                    run_a = _StairsRun.CreateStraightRun(doc, stair_id, line_a, _StairsRunJust.Center)

                    mid_elev_rel = run_a.TopElevation
                    mid_elev_abs = base_z + mid_elev_rel

                    if b_risers <= 0:
                        current_elev_rel = mid_elev_rel
                        continue

                    land_y_bot = flight_y_start_ft + a_run_len
                    land_y_top = land_y_bot + landing_ft
                    lp1 = DB.XYZ(land_x_left,  land_y_bot, mid_elev_abs)
                    lp2 = DB.XYZ(land_x_right, land_y_bot, mid_elev_abs)
                    lp3 = DB.XYZ(land_x_right, land_y_top, mid_elev_abs)
                    lp4 = DB.XYZ(land_x_left,  land_y_top, mid_elev_abs)
                    mid_loop = DB.CurveLoop()
                    mid_loop.Append(DB.Line.CreateBound(lp1, lp2))
                    mid_loop.Append(DB.Line.CreateBound(lp2, lp3))
                    mid_loop.Append(DB.Line.CreateBound(lp3, lp4))
                    mid_loop.Append(DB.Line.CreateBound(lp4, lp1))
                    _StairsLanding.CreateSketchedLanding(doc, stair_id, mid_loop, mid_elev_rel)

                    b_run_len = max(b_risers - 1, 1) * tread_ft
                    p_bs = DB.XYZ(f2_cx, flight_y_start_ft + a_run_len, mid_elev_abs)
                    p_be = DB.XYZ(f2_cx, flight_y_start_ft + a_run_len - b_run_len, mid_elev_abs)
                    line_b = DB.Line.CreateBound(p_bs, p_be)
                    run_b = _StairsRun.CreateStraightRun(doc, stair_id, line_b, _StairsRunJust.Center)
                    current_elev_rel = run_b.TopElevation

                t.Commit()
                t = None
                scope.Commit(NuclearJoinGuard(doc))
                scope = None

                # Post-config
                t2 = DB.Transaction(doc, "AI Stair Config Legacy")
                t2.Start()
                setup_failure_handling(t2)
                stair_el = doc.GetElement(stair_id)
                if stair_el:
                    safe_set_comment(stair_el, tag)
                    try:
                        width_ft = mm_to_ft(width_mm)
                        for sub_id in stair_el.GetStairsRuns():
                            sub_run = doc.GetElement(sub_id)
                            if sub_run:
                                sub_run.ActualRunWidth = width_ft
                    except Exception:
                        pass
                t2.Commit()
                t2.Dispose()
                results["elements"].append(str(stair_id.Value))
                self.log("  Legacy fallback: {} created.".format(tag))

            except Exception as e:
                self.log("  Legacy fallback {} FAILED: {}".format(tag, e))
            finally:
                if t is not None:
                    try: t.RollBack()
                    except Exception: pass
                    try: t.Dispose()
                    except Exception: pass
                if scope is not None:
                    try: scope.RollBack()
                    except Exception: pass
                    try: scope.Dispose()
                    except Exception: pass

    def _create_stair_runs_legacy(self, current_levels, results):
        """LEGACY: Create dogleg staircases one-per-floor using Revit's StairsEditScope.

        Kept as fallback if the batched multi-storey approach fails.

        Layout per dogleg pair (plan view, Y-axis up):
            Main landing (front of shaft) -- CreateSketchedLanding
            Flight A: left side, going +Y
            Mid-landing (U-turn, back of shaft) -- CreateSketchedLanding
            Flight B: right side, going -Y
            [Intermediate landing at front -- repeat for more pairs]

        Riser height is FIXED at spec value.  Revit computes the number
        of risers from the floor height and the fixed riser height.
        Taller floors get more flights (same run length each).

        Stairs are always DELETED and RECREATED -- never reused -- because
        Revit stair elements cannot be edited after StairsEditScope.Commit.
        """
        import Autodesk.Revit.DB as DB  # type: ignore
        from revit_mcp.utils import setup_failure_handling
        import math
        doc = self.doc

        run_data_list = getattr(self, '_stair_run_data', [])
        if not run_data_list:
            self.log("Stair runs: no run data available, skipping.")
            return

        try:
            _StairsEditScope = DB.StairsEditScope
            from Autodesk.Revit.DB.Architecture import (  # type: ignore
                StairsRun as _StairsRun,
                StairsLanding as _StairsLanding,
                StairsRunJustification as _StairsRunJust)
            self.log("Stair runs: native Stairs API loaded OK.")
        except Exception as e:
            self.log("Stair runs: CANNOT load Stairs API: {}".format(e))
            return

        from revit_mcp.preprocessors import NuclearJoinGuard

        # --- Delete ALL existing AI stair elements (always recreate) ---
        deleted_count = 0
        try:
            for el in DB.FilteredElementCollector(doc).OfCategory(
                    DB.BuiltInCategory.OST_Stairs).WhereElementIsNotElementType().ToElements():
                p = el.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if not p:
                    p = el.LookupParameter("Comments")
                if p and p.HasValue:
                    cmt = p.AsString()
                    if cmt and cmt.startswith("AI_") and "Stair_" in cmt:
                        try:
                            t_del = DB.Transaction(doc, "AI Stair Delete")
                            t_del.Start()
                            doc.Delete(el.Id)
                            t_del.Commit()
                            t_del.Dispose()
                            deleted_count += 1
                        except Exception:
                            try:
                                t_del.RollBack()
                            except Exception:
                                pass
        except Exception:
            pass
        if deleted_count:
            self.log("Stair runs: deleted {} old stair elements.".format(deleted_count))

        spec = getattr(self, '_stair_spec', {})
        width_mm = spec.get("width_of_flight", 1500)
        landing_mm = spec.get("landing_width", 1500)
        spec_tread_mm = spec.get("tread", 300)
        spec_riser_mm = spec.get("riser", 150)

        # --- Pre-configure stair type BEFORE creating any stairs ---
        # The first stair in a fresh session uses the template's default
        # riser (e.g. 178mm), not our spec (150mm).  Setting the type
        # param inside StairsEditScope is too late -- Revit already
        # allocated risers with the old value.  Fix: commit the type
        # change in a separate transaction first.
        try:
            stair_types = DB.FilteredElementCollector(doc).OfClass(
                DB.Architecture.StairsType).ToElements()
            if not stair_types:
                stair_types = DB.FilteredElementCollector(doc).OfCategory(
                    DB.BuiltInCategory.OST_Stairs).OfClass(DB.ElementType).ToElements()
            if stair_types:
                t_pre = DB.Transaction(doc, "AI Stair Type Pre-Config")
                t_pre.Start()
                riser_ft = mm_to_ft(spec_riser_mm)
                tread_ft = mm_to_ft(spec_tread_mm)
                for st in stair_types:
                    try:
                        p_riser = st.get_Parameter(
                            DB.BuiltInParameter.STAIRS_ATTR_MAX_RISER_HEIGHT)
                        if p_riser and not p_riser.IsReadOnly:
                            p_riser.Set(riser_ft)
                        for tp in st.GetOrderedParameters():
                            n = tp.Definition.Name.lower()
                            if 'tread' in n and 'depth' in n and not tp.IsReadOnly:
                                tp.Set(tread_ft)
                                break
                    except Exception:
                        pass
                t_pre.Commit()
                t_pre.Dispose()
                self.log("Stair runs: pre-configured {} stair type(s) "
                         "riser={}mm tread={}mm".format(
                    len(stair_types), spec_riser_mm, spec_tread_mm))
        except Exception as e:
            self.log("Stair runs: type pre-config note: {}".format(e))

        created_count = 0

        for rd in run_data_list:
            tag = rd['tag']

            base_idx = rd['base_level_idx']
            top_idx = rd['top_level_idx']
            if base_idx >= len(current_levels) or top_idx >= len(current_levels):
                continue

            base_lvl = current_levels[base_idx]
            top_lvl = current_levels[top_idx]
            floor_h_ft = top_lvl.ProjectElevation - base_lvl.ProjectElevation
            if floor_h_ft <= 0:
                continue
            base_z = base_lvl.ProjectElevation

            num_pairs = rd.get('num_flight_pairs', 1)
            f1 = rd['flight_1']
            f2 = rd['flight_2']
            f1_cx_mm = f1['start'][0]
            f2_cx_mm = f2['start'][0]
            flight_y_start_mm = f1['start'][1]

            scope = None
            t = None
            stair_id = None
            succeeded = False
            try:
                scope = _StairsEditScope(doc, "AI Stair")
                stair_id = scope.Start(base_lvl.Id, top_lvl.Id)

                stair_el = doc.GetElement(stair_id)
                tread_ft = mm_to_ft(spec_tread_mm)
                riser_ft = mm_to_ft(spec_riser_mm)

                t = DB.Transaction(doc, "AI Stair Dogleg")
                t.Start()
                setup_failure_handling(t)

                # --- Push spec riser onto the Revit stair type ---
                if stair_el:
                    try:
                        stair_type = doc.GetElement(stair_el.GetTypeId())
                        if stair_type:
                            p_riser = stair_type.get_Parameter(
                                DB.BuiltInParameter.STAIRS_ATTR_MAX_RISER_HEIGHT)
                            if p_riser and not p_riser.IsReadOnly:
                                p_riser.Set(riser_ft)
                            # Try to set tread depth via name lookup
                            # (BuiltInParameter name varies across Revit versions)
                            for tp in stair_type.GetOrderedParameters():
                                n = tp.Definition.Name.lower()
                                if 'tread' in n and 'depth' in n and not tp.IsReadOnly:
                                    tp.Set(tread_ft)
                                    break
                    except Exception as e:
                        self.log("  Stair type param set note: {}".format(e))

                # --- Geometry from SPEC values (NOT Revit readback) ---
                # Use spec riser to compute total_risers -- must match the
                # num_pairs from staircase_logic which also uses spec riser.
                # Round to nearest mm to avoid float precision errors
                # (e.g. 4200mm stored as 13.7795â€¦ft * 304.8 = 4199.9999â€¦mm)
                floor_h_mm = round(floor_h_ft * 304.8)
                total_risers = int(math.ceil(floor_h_mm / float(spec_riser_mm)))
                rpf = rd.get('risers_per_flight', max(total_risers // 2, 1))

                # Convert mm â†' ft
                f1_cx = mm_to_ft(f1_cx_mm)
                f2_cx = mm_to_ft(f2_cx_mm)
                flight_y_start_ft = mm_to_ft(flight_y_start_mm)
                hw = mm_to_ft(width_mm) / 2.0
                landing_ft = mm_to_ft(landing_mm)

                # Landing X bounds -- span both flights
                land_x_left = f1_cx - hw
                land_x_right = f2_cx + hw

                # --- Distribute risers across flights ---
                # Each flight gets min(rpf, remaining). Last flights may
                # be shorter -- their run_len is computed individually.
                flight_risers_list = []
                remaining = total_risers
                for _ in range(num_pairs * 2):
                    give = min(rpf, remaining)
                    if give <= 0:
                        break
                    flight_risers_list.append(give)
                    remaining -= give

                # Group into pairs: [(a_risers, b_risers), ...]
                flight_pairs = []
                for fi in range(0, len(flight_risers_list), 2):
                    a = flight_risers_list[fi]
                    b = flight_risers_list[fi + 1] if fi + 1 < len(flight_risers_list) else 0
                    if a > 0:
                        flight_pairs.append((a, b))

                self.log("  Stair {}: total_risers={} rpf={} pairs={} "
                         "flights={} floor_h={:.0f}mm".format(
                    tag, total_risers, rpf, len(flight_pairs),
                    flight_risers_list, floor_h_mm))

                # --- Build runs per pair ---
                current_elev_rel = 0.0  # relative to stair base

                for p_idx, (a_risers, b_risers) in enumerate(flight_pairs):
                    current_elev_abs = round(base_z + current_elev_rel, 8)

                    # --- Run A: left side, going +Y ---
                    a_run_len = max(a_risers - 1, 1) * tread_ft
                    p_as = DB.XYZ(round(f1_cx, 8), round(flight_y_start_ft, 8), current_elev_abs)
                    p_ae = DB.XYZ(round(f1_cx, 8), round(flight_y_start_ft + a_run_len, 8), current_elev_abs)
                    line_a = DB.Line.CreateBound(p_as, p_ae)
                    run_a = _StairsRun.CreateStraightRun(doc, stair_id, line_a, _StairsRunJust.Center)

                    mid_elev_rel = a_risers * riser_ft
                    mid_elev_abs = round(base_z + mid_elev_rel, 8)

                    if b_risers <= 0:
                        current_elev_rel = mid_elev_rel
                        continue

                    # --- Mid-landing (U-turn at back of shaft) ---
                    land_y_bot = round(flight_y_start_ft + a_run_len, 8)
                    land_y_top = round(land_y_bot + landing_ft, 8)
                    lp1 = DB.XYZ(round(land_x_left,  8), land_y_bot, mid_elev_abs)
                    lp2 = DB.XYZ(round(land_x_right, 8), land_y_bot, mid_elev_abs)
                    lp3 = DB.XYZ(round(land_x_right, 8), land_y_top, mid_elev_abs)
                    lp4 = DB.XYZ(round(land_x_left,  8), land_y_top, mid_elev_abs)
                    mid_loop = DB.CurveLoop()
                    mid_loop.Append(DB.Line.CreateBound(lp1, lp2))
                    mid_loop.Append(DB.Line.CreateBound(lp2, lp3))
                    mid_loop.Append(DB.Line.CreateBound(lp3, lp4))
                    mid_loop.Append(DB.Line.CreateBound(lp4, lp1))
                    _StairsLanding.CreateSketchedLanding(doc, stair_id, mid_loop, mid_elev_rel)

                    # --- Run B: right side, going -Y ---
                    b_run_len = max(b_risers - 1, 1) * tread_ft
                    p_bs = DB.XYZ(round(f2_cx, 8), land_y_bot, mid_elev_abs)
                    p_be = DB.XYZ(round(f2_cx, 8), round(land_y_bot - b_run_len, 8), mid_elev_abs)
                    line_b = DB.Line.CreateBound(p_bs, p_be)
                    run_b = _StairsRun.CreateStraightRun(doc, stair_id, line_b, _StairsRunJust.Center)

                    current_elev_rel = mid_elev_rel + (b_risers * riser_ft)

                    self.log("  Pair {}/{}: A={} B={} a_run={:.0f} b_run={:.0f}mm "
                             "mid_z={:.0f}mm end_z={:.0f}mm".format(
                        p_idx + 1, len(flight_pairs), a_risers, b_risers,
                        a_run_len * 304.8, b_run_len * 304.8,
                        mid_elev_abs * 304.8,
                        (base_z + current_elev_rel) * 304.8))

                t.Commit()
                t = None

                scope.Commit(NuclearJoinGuard(doc))
                scope = None
                succeeded = True

            except Exception as e:
                self.log("Stair {} FAILED: {}".format(tag, str(e)))
            finally:
                if t is not None:
                    try:
                        t.RollBack()
                    except Exception:
                        pass
                    try:
                        t.Dispose()
                    except Exception:
                        pass
                if scope is not None:
                    try:
                        scope.RollBack()
                    except Exception:
                        pass
                    try:
                        scope.Dispose()
                    except Exception:
                        pass

            if not succeeded:
                continue

            # Post-scope: tag + apply preset width
            t2 = None
            try:
                t2 = DB.Transaction(doc, "AI Stair Config")
                t2.Start()
                setup_failure_handling(t2)
                stair_el = doc.GetElement(stair_id)
                if stair_el:
                    safe_set_comment(stair_el, tag)
                    try:
                        width_ft = mm_to_ft(width_mm)
                        for sub_id in stair_el.GetStairsRuns():
                            sub_run = doc.GetElement(sub_id)
                            if sub_run:
                                sub_run.ActualRunWidth = width_ft
                    except Exception:
                        pass
                t2.Commit()
            except Exception:
                if t2:
                    try:
                        t2.RollBack()
                    except Exception:
                        pass
            finally:
                if t2:
                    try:
                        t2.Dispose()
                    except Exception:
                        pass

            results["elements"].append(str(stair_id.Value))
            created_count += 1

        self.log("Stair runs: {} created, {} deleted".format(
            created_count, deleted_count))
        if self.tracker: self.tracker.record_created("stair_runs", created_count)

    def regenerate_staircases_only(self):
        """Regenerate ONLY the staircase runs using current Revit level heights.
        Uses existing staircase enclosure walls to find the positions, deletes old
        staircase elements, and builds fresh flights adapted to any manually
        changed level heights.
        """
        import Autodesk.Revit.DB as DB # type: ignore
        from revit_mcp.building_generator import get_model_registry
        from revit_mcp.utils import setup_failure_handling, load_presets
        from revit_mcp import staircase_logic
        import re

        doc = self.doc
        registry = get_model_registry(doc)
        
        # 1. Get levels
        levels = []
        for l in DB.FilteredElementCollector(doc).OfClass(DB.Level):
            name = l.Name
            if name.startswith("AI Level") or name.startswith("AI_Level"):
                levels.append(l)
        levels.sort(key=lambda x: x.Elevation)
        
        if len(levels) < 2:
            return {"status": "Error", "message": "Not enough AI Levels found."}
            
        elevations = [l.Elevation for l in levels]
        current_levels = levels
        
        levels_data = []
        for i, elev_ft in enumerate(elevations):
            levels_data.append({
                "id": "AI Level {}".format(i + 1),
                "elevation": elev_ft * 304.8,
            })
            
        # 2. Determine typical height (mode)
        typical_h_mm = 4000.0
        if len(elevations) >= 3:
            sh_list = [round((elevations[i+1]-elevations[i])*304.8) for i in range(len(elevations)-1) if elevations[i+1]-elevations[i] > 0]
            if sh_list:
                typical_h_mm = float(max(set(sh_list), key=sh_list.count))
        elif len(elevations) >= 2:
            typical_h_mm = (elevations[1] - elevations[0]) * 304.8

        # --- AUTO-ADJUST LEVEL HEIGHTS FOR EVEN FLIGHTS ---
        t_levels = DB.Transaction(doc, "AI: Auto-Adjust Levels for Stairs")
        t_levels.Start()
        setup_failure_handling(t_levels)
        needs_commit = False
        
        current_elev = levels[0].Elevation
        for i in range(len(levels) - 1):
            raw_elev_diff_ft = levels[i+1].Elevation - current_elev
            if raw_elev_diff_ft <= 0:
                current_elev = levels[i+1].Elevation
                continue
                
            raw_h_mm = raw_elev_diff_ft * 304.8
            adj_h_mm = staircase_logic.adjust_storey_height(raw_h_mm, typical_h_mm)
            
            if abs(adj_h_mm - raw_h_mm) > 1.0:
                new_elev_ft = current_elev + (adj_h_mm / 304.8)
                levels[i+1].Elevation = new_elev_ft
                levels_data[i+1]['elevation'] = new_elev_ft * 304.8
                self.log("Adjusted Level {} from {:.0f}mm to {:.0f}mm".format(i+2, raw_h_mm, adj_h_mm))
                needs_commit = True
                current_elev = new_elev_ft
            else:
                current_elev = levels[i+1].Elevation
                
        if needs_commit:
            t_levels.Commit()
        else:
            t_levels.RollBack()

        # 3. Find Stair positions by scanning registry for AI_Stair_X_L1_S
        positions = []
        enc_w = 0.0
        stair_tags_found = set()
        
        for tag in registry.keys():
            m = re.match(r'AI_(Stair_\d+)_L1_S', tag)
            if m:
                stair_tags_found.add(m.group(1))
                
        for s_tag in sorted(list(stair_tags_found)):
            e_tag = "AI_{}_L1_E".format(s_tag)
            w_tag = "AI_{}_L1_W".format(s_tag)
            s_wall_tag = "AI_{}_L1_S".format(s_tag)
            n_wall_tag = "AI_{}_L1_N".format(s_tag)
            
            if e_tag in registry and w_tag in registry and s_wall_tag in registry and n_wall_tag in registry:
                e_wall = doc.GetElement(registry[e_tag])
                w_wall = doc.GetElement(registry[w_tag])
                s_wall = doc.GetElement(registry[s_wall_tag])
                n_wall = doc.GetElement(registry[n_wall_tag])
                
                if e_wall and w_wall and s_wall and n_wall:
                    bb_e = e_wall.get_BoundingBox(None)
                    bb_w = w_wall.get_BoundingBox(None)
                    bb_s = s_wall.get_BoundingBox(None)
                    bb_n = n_wall.get_BoundingBox(None)
                    
                    if bb_e and bb_w and bb_s and bb_n:
                        min_x = min(bb_e.Min.X, bb_w.Min.X, bb_s.Min.X, bb_n.Min.X)
                        max_x = max(bb_e.Max.X, bb_w.Max.X, bb_s.Max.X, bb_n.Max.X)
                        min_y = min(bb_e.Min.Y, bb_w.Min.Y, bb_s.Min.Y, bb_n.Min.Y)
                        max_y = max(bb_e.Max.Y, bb_w.Max.Y, bb_s.Max.Y, bb_n.Max.Y)
                        
                        s_cx = (min_x + max_x) / 2.0 * 304.8
                        s_cy = (min_y + max_y) / 2.0 * 304.8
                        width_mm = (max_x - min_x) * 304.8
                        
                        positions.append((s_cx, s_cy))
                        enc_w = max(enc_w, width_mm)
                        
        # --- 3a. Compute Lift Core Bounds [Stair Alignment Fix] ---
        # We need the core bounds to ensure regenerated stairs align correctly
        lift_core_bounds_mm = None
        lx_min, ly_min, lx_max, ly_max = float('inf'), float('inf'), float('-inf'), float('-inf')
        found_lift = False
        for tag, eid in registry.items():
            if "_Lift" in tag and "Wall" in tag:
                found_lift = True
                el = doc.GetElement(eid)
                if not el: continue
                bb = el.get_BoundingBox(None)
                if bb:
                    lx_min = min(lx_min, bb.Min.X * 304.8)
                    ly_min = min(ly_min, bb.Min.Y * 304.8)
                    lx_max = max(lx_max, bb.Max.X * 304.8)
                    ly_max = max(ly_max, bb.Max.Y * 304.8)
        if found_lift:
            lift_core_bounds_mm = (lx_min, ly_min, lx_max, ly_max)

        if not positions:
            return {"status": "Error", "message": "No existing AI staircases found to regenerate."}
            
        stair_spec = getattr(self, "_compliance_params", {})
        
        self.log("Regenerating {} stairs with typical_h_mm={}".format(len(positions), typical_h_mm))
        
        self._stair_run_data = staircase_logic.get_stair_run_data(
            positions, levels_data, enc_w, stair_spec, 
            typical_floor_height_mm=typical_h_mm,
            lift_core_bounds_mm=lift_core_bounds_mm,
            floor_dims_mm=None # floor_dims not easily available here
        )
        stair_spec['_typical_h'] = typical_h_mm
        stair_spec['_rpf'] = staircase_logic._risers_per_flight_typical(
            typical_h_mm, stair_spec.get("riser", 150))
        self._stair_spec = stair_spec

        results = {"elements": []}
        self._create_stair_runs(current_levels, results)

        return {"status": "Success", "message": "Regenerated {} staircases based on new floor heights.".format(len(positions))}

    def _auto_array_windows(self, wall, spacing_mm):
        """Math Delegation: LLM doesn't need to calculate window positions"""
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        spacing = mm_to_ft(spacing_mm)
        lc = wall.Location
        line = lc.Curve
        length = line.Length
        
        if length < spacing: return
        
        count = int(length / spacing)
        symbol = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Windows).OfClass(DB.FamilySymbol).FirstElement()
        if not symbol: return
        if not symbol.IsActive: symbol.Activate()
        
        direction = (line.GetEndPoint(1) - line.GetEndPoint(0)).Normalize()
        for i in range(1, count):
            p = line.GetEndPoint(0) + direction * (i * spacing)
            doc.Create.NewFamilyInstance(p, symbol, wall, doc.GetElement(wall.LevelId), DB.Structure.StructuralType.NonStructural)

    def _find_type(self, category_bip, name):
        import Autodesk.Revit.DB as DB # type: ignore
        cl = DB.FilteredElementCollector(self.doc).OfCategory(category_bip).OfClass(DB.ElementType)
        for t in cl:
            if name.lower() in t.Name.lower(): return t
        return None

    def build_standard_stair(self, data, state):
        """Worker for Vertical Circulation"""
        import Autodesk.Revit.DB as DB # type: ignore
        from Autodesk.Revit.DB.Architecture import StairsRun, StairsRunJustification # type: ignore

        base_lvl_id = DB.ElementId(int(state.get(data['base_level_id'])))
        top_lvl_id = DB.ElementId(int(state.get(data['top_level_id'])))
        loc = DB.XYZ(mm_to_ft(data['x']), mm_to_ft(data['y']), 0)

        # StairsEditScope is in DB, not Architecture
        scope = DB.StairsEditScope(self.doc, "BIM: Stair")
        stair_id = scope.Start(base_lvl_id, top_lvl_id)
        t = DB.Transaction(self.doc, "Stair Run")
        t.Start()
        from revit_mcp.utils import setup_failure_handling
        setup_failure_handling(t)
        try:
            p1 = loc
            p2 = DB.XYZ(loc.X + mm_to_ft(3000), loc.Y, loc.Z)
            line = DB.Line.CreateBound(p1, p2)
            StairsRun.CreateStraightRun(self.doc, stair_id, line, StairsRunJustification.Center)
            t.Commit()
            t.Dispose()
            t = None

            class _FH(DB.IFailuresPreprocessor):
                __namespace__ = "StairFH_legacy"
                def PreprocessFailures(self, accessor):
                    return DB.FailureProcessingResult.Continue
            scope.Commit(_FH())
            scope.Dispose()
            scope = None
            return [{"stair_id": str(stair_id.Value)}]

        except Exception as e:
            if t:
                try: t.RollBack()
                except: pass
                try: t.Dispose()
                except: pass
            if scope:
                try: scope.RollBack()
                except: pass
                try: scope.Dispose()
                except: pass
            raise e
        finally:
            if t is not None:
                try: t.RollBack()
                except: pass
                try: t.Dispose()
                except: pass
            if scope is not None:
                try: scope.RollBack()
                except: pass
                try: scope.Dispose()
                except: pass

    def generate_service_core(self, data, state):
        """Worker for Core Generation"""
        import Autodesk.Revit.DB as DB # type: ignore
        pts = data['boundary_points']
        doc = self.doc
        
        t = DB.Transaction(doc, "BIM: Service Core")
        t.Start()
        from revit_mcp.utils import setup_failure_handling
        setup_failure_handling(t)
        try:
            # 1. Create reinforced concrete walls
            wt = DB.FilteredElementCollector(doc).OfClass(DB.WallType).FirstElement()
            lvl = doc.ActiveView.GenLevel
            curve_loop = DB.CurveLoop()
            
            for i in range(len(pts)):
                p1 = DB.XYZ(mm_to_ft(pts[i]['x']), mm_to_ft(pts[i]['y']), 0)
                p2 = DB.XYZ(mm_to_ft(pts[(i+1)%len(pts)]['x']), mm_to_ft(pts[(i+1)%len(pts)]['y']), 0)
                line = DB.Line.CreateBound(p1, p2)
                wall = DB.Wall.Create(doc, line, wt.Id, lvl.Id, mm_to_ft(20000), 0, False, False)
                disallow_joins(wall)
                curve_loop.Append(line)
            
            # 2. Shaft Opening
            loops = [curve_loop]
            DB.Opening.CreateShaft(doc, lvl.Id, lvl.Id, curve_loop) # Simplified
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        return {"success": True}

    def generate_curtain_facade(self, data, state):
        """Worker for Curtain Systems"""
        import Autodesk.Revit.DB as DB # type: ignore
        wall = self.doc.GetElement(DB.ElementId(int(data['wall_id'])))
        
        t = DB.Transaction(self.doc, "BIM: Curtain Facade")
        t.Start()
        from revit_mcp.utils import setup_failure_handling
        setup_failure_handling(t)
        try:
            # Change wall type to Curtain Wall
            cw_type = None
            for wt in DB.FilteredElementCollector(self.doc).OfClass(DB.WallType):
                if wt.Kind == DB.WallKind.Curtain:
                    cw_type = wt; break
            if cw_type: wall.WallType = cw_type
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        return {"success": True}

    def create_parametric_roof(self, data, state):
        """Worker for Roof Generation"""
        import Autodesk.Revit.DB as DB # type: ignore
        import System.Collections.Generic as Generic # type: ignore
        
        pts = data['boundary_points']
        lvl = self.doc.ActiveView.GenLevel
        
        t = DB.Transaction(self.doc, "BIM: Roof")
        t.Start()
        from revit_mcp.utils import setup_failure_handling
        setup_failure_handling(t)
        try:
            footprint = DB.CurveArray()
            for i in range(len(pts)):
                p1 = DB.XYZ(mm_to_ft(pts[i]['x']), mm_to_ft(pts[i]['y']), 0)
                p2 = DB.XYZ(mm_to_ft(pts[(i+1)%len(pts)]['x']), mm_to_ft(pts[(i+1)%len(pts)]['y']), 0)
                footprint.Append(DB.Line.CreateBound(p1, p2))
            
            mapping = DB.ModelCurveArray()
            roof = self.doc.Create.NewFootprintRoof(footprint, lvl, DB.FilteredElementCollector(self.doc).OfClass(DB.RoofType).FirstElement(), mapping)
            for curve in mapping:
                roof.set_DefinesSlope(curve, True)
                roof.set_Slope(curve, data.get('slope', 30.0) * (3.14159 / 180.0))
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        return {"roof_id": str(roof.Id.Value)}

    def perform_global_cleanup(self):
        """Worker for BIM Health: Targeted Join for AI elements only"""
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        t = DB.Transaction(doc, "BIM: Targeted Global Cleanup")
        t.Start()
        from revit_mcp.utils import setup_failure_handling
        setup_failure_handling(t)
        try:
            # OPTIMIZATION: Instead of scanning the entire document, only scan AI elements
            # This is significantly faster in large projects.
            from revit_mcp.building_generator import get_model_registry
            registry = get_model_registry(doc)
            ai_ids = [eid for eid in registry.values()]
            
            if not ai_ids: 
                t.RollBack()
                return {"status": "No AI elements to cleanup"}
                
            from System.Collections.Generic import List
            ids_list = List[DB.ElementId]()
            for eid in ai_ids: ids_list.Add(eid)
            
            if hasattr(self, "_affected_elements") and self._affected_elements:
                ai_elements = self._affected_elements
            else:
                ai_elements = DB.FilteredElementCollector(doc, ids_list).WhereElementIsNotElementType().ToElements()
            
            # ONLY cleanup shell elements (Walls/Floors). Skip Lifts for speed.
            walls = []
            for e in ai_elements:
                if isinstance(e, DB.Wall):
                    p = e.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                    if p and p.HasValue and "Lift" in p.AsString(): continue 
                    walls.append(e)
            
            floors = [e for e in ai_elements if isinstance(e, DB.Floor)]
            
            # NO AUTOMATIC JOINING: User requested 100% disjoint mode.
            # 1. Targeted Join: Walls with Floors - REMOVED for performance and user request
            pass
            
            # 2. Wall-Wall joins: REMOVED O(N^2) Manual Join
            # Revit's native AutoJoin handles this much more efficiently during Transaction.Commit
            # when SetAllowAutoJoin(True) is called.
            
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        return {"status": "Targeted Cleanup completed successfully"}

    def generate_submission_set(self):
        """Worker for Documentation"""
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        
        t = DB.Transaction(doc, "BIM: Documentation")
        t.Start()
        from revit_mcp.utils import setup_failure_handling
        setup_failure_handling(t)
        try:
            # 1. Create Views
            lvls = DB.FilteredElementCollector(doc).OfClass(DB.Level).ToElements()
            vt = None
            for f in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType):
                if f.ViewFamily == DB.ViewFamily.FloorPlan:
                    vt = f; break
            
            plans = []
            existing_plans = DB.FilteredElementCollector(doc).OfClass(DB.ViewPlan).ToElements()
            
            for lvl in lvls:
                # Check if a floor plan already exists for this level
                existing = None
                for ep in existing_plans:
                    if ep.ViewType == DB.ViewType.FloorPlan and ep.GenLevel and ep.GenLevel.Id == lvl.Id:
                        existing = ep
                        break
                        
                if existing:
                    plans.append(existing)
                    continue
                    
                v = DB.ViewPlan.Create(doc, vt.Id, lvl.Id)
                plans.append(v)
                # 1.5 Auto-Dimension Grids
                try: self._dimension_grids_in_view(v)
                except: pass
            
            # 2. Create Sheet
            tb = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_TitleBlocks).FirstElementId()
            sheet = DB.ViewSheet.Create(doc, tb)
            sheet.Name = "SUBMISSION SET"
            
            # 3. Simple Placement
            pt = DB.XYZ(0, 0, 0)
            if plans:
                try:
                    # Viewport.Create throws ArgumentException if the view is already placed
                    DB.Viewport.Create(doc, sheet.Id, plans[0].Id, pt)
                except:
                    pass
            
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        return {"sheet_id": str(sheet.Id.Value)}

    def _dimension_grids_in_view(self, view):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        
        # 1. Cleanup old AI dimensions in this view
        dim_count = 0
        try:
            old_dims = DB.FilteredElementCollector(doc, view.Id).OfClass(DB.Dimension).ToElements()
            for od in old_dims:
                p = od.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
                if p and "AI_Dim" in p.AsString():
                    doc.Delete(od.Id)
                    dim_count += 1
        except: pass
        if dim_count > 0: self.log("RevitWorkers: Cleaned up {} old AI dimensions.".format(dim_count))

        # 2. Collect Grids once
        grids = list(DB.FilteredElementCollector(doc, view.Id).OfClass(DB.Grid))
        if len(grids) < 2: return
        
        x_grids = []
        y_grids = []
        for g in grids:
            curve = g.Curve
            if not isinstance(curve, DB.Line): continue
            direction = curve.Direction
            if abs(direction.Y) > 0.9: x_grids.append(g)
            elif abs(direction.X) > 0.9: y_grids.append(g)

        def create_dim(view, grid_list, offset, is_overall=False):
            if len(grid_list) < 2: return
            refs = DB.ReferenceArray()
            
            # Sort by coordinate
            is_x_dim = abs(offset.X) > 0.1 # This is a Y-Grid dimension (horizontal chain)
            grid_list.sort(key=lambda g: g.Curve.GetEndPoint(0).X if not is_x_dim else g.Curve.GetEndPoint(0).Y)
            
            if is_overall:
                refs.Append(DB.Reference(grid_list[0]))
                refs.Append(DB.Reference(grid_list[-1]))
            else:
                for g in grid_list: refs.Append(DB.Reference(g))
            
            # Placement line
            p1 = grid_list[0].Curve.GetEndPoint(0) + offset
            p2 = grid_list[-1].Curve.GetEndPoint(0) + offset
            if p1.DistanceTo(p2) < mm_to_ft(2.0): return 
            line = DB.Line.CreateBound(p1, p2)
            
            try:
                dim = doc.Create.NewDimension(view, line, refs)
                safe_set_comment(dim, "AI_Dim")
            except: pass

        # For Floor Plans: X-Grids (Vertical) vs Y-Grids (Horizontal)
        # For Section Views: Only vertical lines (grids seen in projection) are visible.
        
        is_plan = view.ViewType == DB.ViewType.FloorPlan
        
        if is_plan:
            create_dim(view, x_grids, DB.XYZ(0, -mm_to_ft(3000), 0), is_overall=False)
            create_dim(view, x_grids, DB.XYZ(0, -mm_to_ft(5000), 0), is_overall=True)
            create_dim(view, y_grids, DB.XYZ(-mm_to_ft(3000), 0, 0), is_overall=False)
            create_dim(view, y_grids, DB.XYZ(-mm_to_ft(5000), 0, 0), is_overall=True)
        else:
            # Section view: All visible grids are vertical lines in the view plane.
            # Combine all for a single horizontal dimension chain.
            v_grids = x_grids + y_grids
            if len(v_grids) >= 2:
                # Offset in the view's current Up direction
                up = view.UpDirection.Normalize() * mm_to_ft(3000)
                create_dim(view, v_grids, up, is_overall=False)
                create_dim(view, v_grids, up + view.UpDirection.Normalize() * mm_to_ft(2000), is_overall=True)

    def _dimension_levels_in_view(self, view):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        levels = list(DB.FilteredElementCollector(doc).OfClass(DB.Level))
        if len(levels) < 2: return
        
        # Sort by elevation
        levels.sort(key=lambda l: l.Elevation)
        
        refs = DB.ReferenceArray()
        for l in levels: refs.Append(DB.Reference(l))
        
        # Vertical placement line (offset left of building)
        offset_x = -mm_to_ft(5000)
        p1 = DB.XYZ(offset_x, 0, levels[0].Elevation)
        p2 = DB.XYZ(offset_x, 0, levels[-1].Elevation)
        if p1.DistanceTo(p2) < mm_to_ft(2.0): return 
        line = DB.Line.CreateBound(p1, p2)
        
        try:
            dim = doc.Create.NewDimension(view, line, refs)
            safe_set_comment(dim, "AI_Dim")
            
            # Overall height
            refs_o = DB.ReferenceArray()
            refs_o.Append(DB.Reference(levels[0]))
            refs_o.Append(DB.Reference(levels[-1]))
            line_o = DB.Line.CreateBound(p1 + DB.XYZ(-mm_to_ft(2000),0,0), p2 + DB.XYZ(-mm_to_ft(2000),0,0))
            dim_o = doc.Create.NewDimension(view, line_o, refs_o)
            safe_set_comment(dim_o, "AI_Dim")
        except: pass

    def _create_or_update_section(self, name, center, basis_x, basis_y, basis_z, width, height, far_clip):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        
        # 1. Find or Create Section View
        view = None
        for v in DB.FilteredElementCollector(doc).OfClass(DB.ViewSection):
            if v.Name == name:
                view = v; break
        
        # 2. Bounding Box for Section
        # This defines the view's coordinate system and crop region
        bbox = DB.BoundingBoxXYZ()
        bbox.Enabled = True
        bbox.Transform = DB.Transform.Identity
        bbox.Transform.Origin = center
        bbox.Transform.BasisX = basis_x
        bbox.Transform.BasisY = basis_y
        bbox.Transform.BasisZ = basis_z
        
        # Extents (in internal coordinates of the BBox)
        bbox.Min = DB.XYZ(-width/2.0, -height/2.0, -far_clip)
        bbox.Max = DB.XYZ(width/2.0, height/2.0, 0)
        
        if not view:
            # Find Section ViewFamilyType
            vft = None
            for vfam in DB.FilteredElementCollector(doc).OfClass(DB.ViewFamilyType):
                if vfam.ViewFamily == DB.ViewFamily.Section:
                    vft = vfam; break
            if not vft: return None
            view = DB.ViewSection.CreateSection(doc, vft.Id, bbox)
            view.Name = name
        else:
            # Update Crop Region (This is complex in Revit, but we can update the Section Box if we had the original tag)
            # For simplicity, we just reuse the existing view with its previous box unless user asks for re-centering.
            pass
            
        return view

    def polish_model(self):
        """
        BIM Polish: The 'Denuclearization' command.
        Restores joins for all AI elements to finalize the model visuals.
        """
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        from revit_mcp.building_generator import get_model_registry
        registry = get_model_registry(doc)
        
        t = DB.Transaction(doc, "BIM: Final Polish (Restoring Joins)")
        t.Start()
        # NOTE: We do NOT use the JoinGuard here, as we WANT the joined results.
        try:
            count = 0
            for tag, eid in registry.items():
                el = doc.GetElement(eid)
                if el and isinstance(el, DB.Wall):
                    # 1. Re-enable AutoJoin property
                    el.SetAllowAutoJoin(True)
                    # 2. Re-allow end joins (The 'Blue Dot' action)
                    DB.WallUtils.AllowWallJoinAtEnd(el, 0)
                    DB.WallUtils.AllowWallJoinAtEnd(el, 1)
                    count += 1
            
            # 3. Force a global join pass for Walls/Floors
            self.perform_global_cleanup() 
            
            t.Commit()
            self.log("BIM Polish complete: Restored joins for {} walls. Model is now in 'Final Mode'.".format(count))
            return {"status": "Success", "walls_processed": count}
        except Exception as e:
            t.RollBack()
            return {"error": str(e)}


def execute_in_transaction_group(doc, name, action_func):
    import Autodesk.Revit.DB as DB # type: ignore
    tg = DB.TransactionGroup(doc, name)
    tg.Start()
    try:
        # We might have nested individual transactions inside workers
        result = action_func()
        tg.Assimilate()
        return result
    except Exception as e:
        tg.RollBack()
        raise e
