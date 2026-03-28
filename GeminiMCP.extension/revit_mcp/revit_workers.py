# -*- coding: utf-8 -*-
# NOTE: Do NOT import Autodesk.Revit.DB at module level.
# Each method does its own local import on the correct thread.
from revit_mcp.gemini_client import client
from revit_mcp.bridge import mcp_event_handler
from revit_mcp.utils import (
    safe_num, mm_to_ft, sqmm_to_sqft, 
    safe_set_comment, get_location_line_param,
    get_random_dim, load_presets, ft_to_mm
)
from . import lift_logic
import math

class RevitWorkers:
    def __init__(self, doc):
        self.doc = doc

    def log(self, message):
        client.log(message)

    def execute_fast_manifest(self, manifest):
        """High-speed execution using intent-based logic and state-aware updates"""
        self.log("--- execute_fast_manifest START ---")
        import Autodesk.Revit.DB as DB # type: ignore
        from revit_mcp.building_generator import get_model_registry # type: ignore
        doc = self.doc
        results = {"levels": [], "elements": [], "summary": {}}
        
        # 1. State Scan
        registry = get_model_registry(doc)
        
        # 2. Context Initialization
        reused, created, deleted = [0], [0], [0]
        
        tg = DB.TransactionGroup(doc, "AI Build: Fast Manifest")
        tg.Start()
        
        t = DB.Transaction(doc, "AI Build: Execute Manifest")
        t.Start()
        
        # Track elements that actually changed for efficient re-join
        affected_elements = []

        try:
            # 3. Processing Pipeline
            elevations, current_levels = self._process_levels(manifest, registry, created, reused)
            results["levels"] = [str(l.Id.Value) for l in current_levels]
            
            floor_dims, shell = self._process_shell_dimensions(manifest, current_levels, registry)
            
            # --- EXPAND LIFTS FIRST (to anchor grid) ---
            self.log("Step 3a: Expanding Vertical Circulation (Lifts)...")
            core_bounds = self._expand_lifts_in_manifest(manifest, current_levels, elevations, floor_dims, affected_elements)
            
            updated_walls = self._process_walls(current_levels, elevations, floor_dims, registry, results, created, reused, affected_elements)
            
            expanded_slab_dims = self._process_floors(current_levels, floor_dims, shell, registry, results, created, reused, affected_elements)
            
            self._process_parapets(current_levels, expanded_slab_dims, floor_dims, shell, registry, results, created, reused, affected_elements)
            
            max_w = max(d[0] for d in floor_dims)
            max_l = max(d[1] for d in floor_dims)
            
            self.log("Step 3b: Processing Structure with Core Alignment...")
            self._process_columns_and_grids(current_levels, elevations, floor_dims, expanded_slab_dims, shell, registry, results, created, reused, affected_elements, core_bounds)
            
            # --- GRANULAR OVERRIDES ---
            self.log("Step 4: Processing Granular Element Overrides...")
            self._process_granular_walls(manifest, current_levels, registry, results, created, reused, affected_elements)
            self._process_granular_floors(manifest, current_levels, registry, results, created, reused, affected_elements)
            self._process_granular_columns(manifest, current_levels, registry, results, created, reused)
            
            # 4. Cleanup & Documentation
            self._cleanup_registry(registry, results, deleted)
            self._generate_documentation(current_levels, elevations, floor_dims, max_w, max_l)
            
            # 5. Finalize: Selective Re-Join (DISABLED for Draft Mode speed)
            # We skip re-enabling AutoJoin here to keep editing nearly instantaneous.
            # Lifts and Shell walls remain in their 'disjoint' state until a Polish command is run.
            
            # Store for potential future cleanup
            self._affected_elements = [el for el in affected_elements if el and el.IsValidObject]
            
            t.Commit()
            tg.Assimilate()
            
            results["summary"] = {"reused": reused[0], "created": created[0], "deleted": deleted[0]}
            self.log("Fast-Track Summary: {}".format(results["summary"]))
            return results
            
        except Exception as e:
            import traceback
            self.log("CRITICAL ERROR in execute_fast_manifest: {}\n{}".format(str(e), traceback.format_exc()))
            t.RollBack()
            tg.RollBack()
            return {"error": str(e)}

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
        
        if isinstance(levels_val, list):
            elevations = [mm_to_ft(e) for e in levels_val]
        else:
            count = int(safe_num(levels_val, 1))
            elevations = [0.0]
            curr = 0.0
            for i in range(1, count + 1):
                h_val = height_overrides.get(str(i), height_val)
                h = get_random_dim(h_val, default_height, variation=0.15) if h_val == "random" else safe_num(h_val, default_height)
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
        
        return elevations, current_levels
    def _process_shell_dimensions(self, manifest, current_levels, registry):
        doc = self.doc
        shell = manifest.get("shell", {})
        w_def, l_def = 30000.0, 50000.0
        
        # 1. Base Dimension Logic:
        # Use explicit shell dimensions or fallback to 30m x 50m default.
        m_w, m_l = shell.get("width"), shell.get("length")
        base_w = safe_num(m_w, w_def)
        base_l = safe_num(m_l, l_def)
        overrides = shell.get("floor_overrides", {})
        
        dims = []
        for i in range(len(current_levels)):
            lvl_idx = i + 1
            ov = overrides.get(str(lvl_idx), {})
            
            # 1. Start with Override or Global Shell
            w = ov.get("width", shell.get("width"))
            l = ov.get("length", shell.get("length"))
            
            # 2. Handle 'random' BEFORE state inference to ensure variety
            final_w = get_random_dim(w, base_w, variation=0.25)
            final_l = get_random_dim(l, base_l, variation=0.25)
            
            # 3. If completely missing in manifest (None after random check), try to INFER
            if w is None or l is None:
                # Try specific Level tags
                w_tag, l_tag = f"AI_Wall_L{lvl_idx}_S", f"AI_Wall_L{lvl_idx}_W"
                f_tag = f"AI_Floor_L{lvl_idx}"
                
                # Check walls at this level
                if w_tag in registry and l_tag in registry:
                    w_el, l_el = doc.GetElement(registry[w_tag]), doc.GetElement(registry[l_tag])
                    if w_el and l_el and hasattr(w_el.Location, "Curve") and hasattr(l_el.Location, "Curve"):
                        if w is None: final_w = w_el.Location.Curve.Length * 304.8
                        if l is None: final_l = l_el.Location.Curve.Length * 304.8
                
                # If still missing, check Floor at this level
                if (w is None or l is None) and f_tag in registry:
                    floor = doc.GetElement(registry[f_tag])
                    if floor and hasattr(floor, "get_BoundingBox"):
                        bb = floor.get_BoundingBox(None)
                        if bb:
                            if w is None: final_w = (bb.Max.X - bb.Min.X) * 304.8
                            if l is None: final_l = (bb.Max.Y - bb.Min.Y) * 304.8
            
            dims.append((final_w, final_l))
        return dims, shell

    def _process_walls(self, current_levels, elevations, floor_dims, registry, results, created, reused, affected_elements):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        updated = []
        tags = ["AI_Wall_L{}_N", "AI_Wall_L{}_E", "AI_Wall_L{}_S", "AI_Wall_L{}_W"]
        
        for k, lvl in enumerate(current_levels):
            w_k, l_k = floor_dims[k]
            w_k_ft, l_k_ft = mm_to_ft(w_k), mm_to_ft(l_k)
            pts = [DB.XYZ(-w_k_ft/2, -l_k_ft/2, 0), DB.XYZ(w_k_ft/2, -l_k_ft/2, 0), DB.XYZ(w_k_ft/2, l_k_ft/2, 0), DB.XYZ(-w_k_ft/2, l_k_ft/2, 0)]
            
            for j in range(4):
                tag = tags[j].format(k+1)
                p1 = DB.XYZ(pts[j].X, pts[j].Y, elevations[k])
                p2 = DB.XYZ(pts[(j+1)%4].X, pts[(j+1)%4].Y, elevations[k])
                
                if p1.DistanceTo(p2) < mm_to_ft(2.0): continue
                line = DB.Line.CreateBound(p1, p2)
                
                wall_id = registry.get(tag)
                wall = doc.GetElement(wall_id) if wall_id else None
                
                is_changed = False
                if wall and isinstance(wall, DB.Wall):
                    if hasattr(wall, "SetAllowAutoJoin"): wall.SetAllowAutoJoin(False)
                    # Use endpoint comparison since Line doesn't have IsSimilar
                    w_curve = wall.Location.Curve
                    if not (w_curve.GetEndPoint(0).IsAlmostEqualTo(line.GetEndPoint(0)) and \
                            w_curve.GetEndPoint(1).IsAlmostEqualTo(line.GetEndPoint(1))):
                        wall.Location.Curve = line
                        is_changed = True
                    reused[0] += 1
                else:
                    wall = DB.Wall.Create(doc, line, lvl.Id, False)
                    if hasattr(wall, "SetAllowAutoJoin"): wall.SetAllowAutoJoin(False)
                    safe_set_comment(wall, tag)
                    created[0] += 1
                    is_changed = True
                
                if is_changed:
                    affected_elements.append(wall)
                
                # Height & Constraints
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
            if hasattr(wall, "SetAllowAutoJoin"): wall.SetAllowAutoJoin(False)
            w_curve = wall.Location.Curve
            if not (w_curve.GetEndPoint(0).IsAlmostEqualTo(line.GetEndPoint(0)) and \
                    w_curve.GetEndPoint(1).IsAlmostEqualTo(line.GetEndPoint(1))):
                wall.Location.Curve = line
                is_changed = True
            reused[0] += 1
        else:
            wall = DB.Wall.Create(doc, line, wall_type.Id, lvl.Id, height, 0, False, False)
            if hasattr(wall, "SetAllowAutoJoin"): wall.SetAllowAutoJoin(False)
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
        
        # Level k (starting from Level 1, we check edges)
        for k in range(len(current_levels)):
            lvl = current_levels[k]
            
            # 1. Determine Parapet Height for this level
            lvl_ov = overrides.get(str(k+1), {})
            p_h_mm = safe_num(lvl_ov.get("parapet_height", g_parapet_h), 1000)
            if p_h_mm <= 0: continue # Explicitly disabled
            
            slab_w, slab_l = expanded_slab_dims[k]
            w_above, l_above = (0, 0)
            if k < len(floor_dims): # Storey above exists
                w_above, l_above = floor_dims[k]
            
            # If slab is larger than walls above, generate parapets for safety
            if slab_w > w_above + 10 or slab_l > l_above + 10: # 10mm tolerance
                sw_ft, sl_ft = mm_to_ft(slab_w), mm_to_ft(slab_l)
                wa_ft, la_ft = mm_to_ft(w_above), mm_to_ft(l_above)
                
                # Boundary of the slab
                pts = [DB.XYZ(-sw_ft/2.0, -sl_ft/2.0, 0), 
                       DB.XYZ(sw_ft/2.0, -sl_ft/2.0, 0), 
                       DB.XYZ(sw_ft/2.0, sl_ft/2.0, 0), 
                       DB.XYZ(-sw_ft/2.0, sl_ft/2.0, 0)]
                
                wall_type = DB.FilteredElementCollector(doc).OfClass(DB.WallType).FirstElement()
                for i in range(4):
                    p1, p2 = pts[i], pts[(i+1)%4]
                    # Check if this edge is exposed (outside walls above)
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
        
        # 1. SHELTER RULE: Slab at Level k = max(Storey_{k-1}, Storey_k)
        expanded_slab_dims = []
        g_c_depth = shell.get("cantilever_depth")
        overrides = shell.get("floor_overrides", {})
        
        for k in range(len(current_levels)):
            w_below = floor_dims[k-1][0] if k > 0 else 0
            l_below = floor_dims[k-1][1] if k > 0 else 0
            w_above = floor_dims[k][0] if k < len(floor_dims) else 0
            l_above = floor_dims[k][1] if k < len(floor_dims) else 0
            
            # Shelter Rule: Slab covers its own floor AND the floor below
            base_slab_w = max(w_below, w_above)
            base_slab_l = max(l_below, l_above)
            
            # Cantilever Logic: Add extra depth if requested
            lvl_ov = overrides.get(str(k+1), {})
            c_val = lvl_ov.get("cantilever_depth", g_c_depth)
            # Default to 0 unless 'random' is explicitly requested
            c_depth = get_random_dim(c_val, 1500, variation=0.5) if c_val == "random" else safe_num(c_val, 0)
            
            slab_w = base_slab_w + (c_depth * 2)
            slab_l = base_slab_l + (c_depth * 2)
            
            expanded_slab_dims.append((slab_w, slab_l))

        for k, lvl in enumerate(current_levels):
            slab_w, slab_l = expanded_slab_dims[k]
            tag = "AI_Floor_L{}".format(k+1)
            
            w_ft, l_ft = mm_to_ft(slab_w), mm_to_ft(slab_l)
            p1 = DB.XYZ(-w_ft/2.0, -l_ft/2.0, 0)
            p2 = DB.XYZ(w_ft/2.0, -l_ft/2.0, 0)
            p3 = DB.XYZ(w_ft/2.0, l_ft/2.0, 0)
            p4 = DB.XYZ(-w_ft/2.0, l_ft/2.0, 0)
            
            loop = DB.CurveLoop()
            loop.Append(DB.Line.CreateBound(p1, p2))
            loop.Append(DB.Line.CreateBound(p2, p3))
            loop.Append(DB.Line.CreateBound(p3, p4))
            loop.Append(DB.Line.CreateBound(p4, p1))
            
            floor = doc.GetElement(registry[tag]) if tag in registry else None
            needs_create = True
            if floor and isinstance(floor, DB.Floor):
                try:
                    sb = floor.GetSlabShapeEditor()
                    if sb: sb.ResetSlabShape()
                    import System.Collections.Generic as Generic
                    loops = Generic.List[DB.CurveLoop]()
                    loops.Add(loop)
                    floor.SetBoundary(loops)
                    needs_create = False
                    affected_elements.append(floor)
                except Exception:
                    try: doc.Delete(floor.Id)
                    except: pass
            
            if needs_create:
                import System.Collections.Generic as Generic # type: ignore
                loops = Generic.List[DB.CurveLoop]()
                loops.Add(loop)
                floor = DB.Floor.Create(doc, loops, ftype.Id, lvl.Id)
                safe_set_comment(floor, tag)
                affected_elements.append(floor)
            
            if tag not in registry: created[0] += 1
            else: reused[0] += 1
            results["elements"].append(str(floor.Id.Value))
        
        return expanded_slab_dims

    def _process_columns_and_grids(self, current_levels, elevations, floor_dims, expanded_slab_dims, shell, registry, results, created, reused, affected_elements=[], core_bounds=None):
        """Standard high-speed structural layout with stable grid anchoring.
        If core_bounds is provided, it anchors the grid on the core center and culls columns inside."""
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        
        # 1. Logic Setup: Span & Phase
        col_span = safe_num(shell.get("column_span", shell.get("column_spacing")), 10000.0)
        
        # Detect existing span from registry or model
        existing_pts = []
        if registry:
            for tag, eid in registry.items():
                if tag.startswith("AI_Col_L1_"):
                    el = doc.GetElement(eid)
                    if el and hasattr(el.Location, "Point"): existing_pts.append(el.Location.Point)
        
        if col_span is None or col_span == 0:
            if len(existing_pts) >= 2:
                # Infer existing span
                pts_sorted = sorted(existing_pts, key=lambda p: (round(p.X, 2), round(p.Y, 2)))
                for i in range(len(pts_sorted)-1):
                    d = pts_sorted[i].DistanceTo(pts_sorted[i+1])
                    if d > 1.0: # at least 300mm
                        detected_mm = round(d * 304.8 / 100.0) * 100.0
                        if detected_mm > 2000:
                            col_span = detected_mm
                            break
            # Final fallback
            if not col_span: col_span = 10000.0

        is_structural = True
        symbol = self._find_type(DB.BuiltInCategory.OST_StructuralColumns, shell.get("column_type", "Structural Column"))
        if not symbol:
            symbol = DB.FilteredElementCollector(doc).OfCategory(DB.BuiltInCategory.OST_Columns).OfClass(DB.FamilySymbol).FirstElement()
            is_structural = False
        if not symbol: return
        if not symbol.IsActive: symbol.Activate()
        
        target_span_mm = round(col_span / 100.0) * 100.0
        span_ft = mm_to_ft(target_span_mm)
        col_margin_ft = mm_to_ft(400)
        
        max_w = max(d[0] for d in floor_dims)
        max_l = max(d[1] for d in floor_dims)
        center_only = shell.get("columns_center_only", False) or "center area" in str(shell).lower()

        # Anchoring: Anchor on Core Center if available, else (0,0)
        anchor_ft_x = 0.0
        anchor_ft_y = 0.0
        if core_bounds:
            anchor_ft_x = (core_bounds[0] + core_bounds[2]) / 2.0
            anchor_ft_y = (core_bounds[1] + core_bounds[3]) / 2.0

        # 2. STABLE GRID LOGIC: Anchor on core/center
        def get_stable_offsets(dim_mm, span_ft, anchor_ft, center_only=False):
            half_dim_ft = mm_to_ft(dim_mm) / 2.0
            offsets = []
            curr = anchor_ft
            while curr <= half_dim_ft + 0.1:
                offsets.append(curr)
                curr += span_ft
            curr = anchor_ft - span_ft
            while curr >= -half_dim_ft - 0.1:
                offsets.append(curr)
                curr -= span_ft
            
            clean_offsets = sorted(list(set(round(o, 4) for o in offsets)))
            if center_only and len(clean_offsets) >= 3:
                return [o for o in clean_offsets if abs(o) < half_dim_ft - 0.1]
            return clean_offsets

        x_offsets = get_stable_offsets(max_w, span_ft, anchor_ft_x, center_only)
        y_offsets = get_stable_offsets(max_l, span_ft, anchor_ft_y, center_only)
        
        # 3. PILLAR RULE with Stable Mapping
        max_level_for_grid = {} 
        for ox in x_offsets:
            for oy in y_offsets:
                # 1. CULLING: Don't place columns inside core bounding box
                if core_bounds:
                    m = mm_to_ft(500) # margin
                    if (core_bounds[0] - m <= ox <= core_bounds[2] + m) and \
                       (core_bounds[1] - m <= oy <= core_bounds[3] + m):
                        continue

                # Indices for tagging: normalize to span relative to anchor
                ix = int(round((ox - anchor_ft_x) / span_ft))
                iy = int(round((oy - anchor_ft_y) / span_ft))
                
                k_highest = -1
                for k in range(len(current_levels)):
                    sw, sl = expanded_slab_dims[k]
                    if abs(ox) <= (mm_to_ft(sw)/2.0 - col_margin_ft/2.0) and abs(oy) <= (mm_to_ft(sl)/2.0 - col_margin_ft/2.0):
                        k_highest = k
                if k_highest >= 0:
                    max_level_for_grid[(ix, iy, ox, oy)] = k_highest

        for (ix, iy, ox, oy), k_max in max_level_for_grid.items():
            for k in range(k_max):
                tag = "AI_Col_L{}_GX{}_GY{}".format(k+1, ix, iy)
                p = DB.XYZ(ox, oy, 0)
                lvl = current_levels[k]
                
                col = doc.GetElement(registry[tag]) if tag in registry else None
                if not col:
                    st = DB.Structure.StructuralType.Column if is_structural else DB.Structure.StructuralType.NonStructural
                    col = doc.Create.NewFamilyInstance(p, symbol, lvl, st)
                    safe_set_comment(col, tag)
                    created[0] += 1
                else:
                    col.Location.Point = p
                    safe_set_comment(col, tag)
                    reused[0] += 1
                
                try:
                    bip = DB.BuiltInParameter.FAMILY_TOP_LEVEL_PARAM if is_structural else DB.BuiltInParameter.COLUMN_TOP_LEVEL_PARAM
                    param = col.get_Parameter(bip)
                    if param: param.Set(current_levels[k+1].Id)
                except: pass
                
                results["elements"].append(str(col.Id.Value))
                affected_elements.append(col)

    def _process_granular_walls(self, manifest, current_levels, registry, results, created, reused, affected_elements=[]):
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        level_map = {l.Name: l for l in current_levels}
        for l in current_levels:
            p = l.get_Parameter(DB.BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p and p.HasValue: level_map[p.AsString()] = l

        granular_walls = []
        for w_data in manifest.get("walls", []):
            ai_id = w_data.get("id")
            if not ai_id: continue
            
            p1_raw = w_data.get("start", [0, 0, 0])
            p2_raw = w_data.get("end", [1000, 0, 0])
            p1 = DB.XYZ(mm_to_ft(p1_raw[0]), mm_to_ft(p1_raw[1]), mm_to_ft(p1_raw[2]))
            p2 = DB.XYZ(mm_to_ft(p2_raw[0]), mm_to_ft(p2_raw[1]), mm_to_ft(p2_raw[2]))
            line = DB.Line.CreateBound(p1, p2)
            
            lvl = level_map.get(w_data.get("level_id"))
            if not lvl: lvl = current_levels[0]
            
            wall = doc.GetElement(registry[ai_id]) if ai_id in registry else None
            is_changed = False
            if wall and isinstance(wall, DB.Wall):
                if hasattr(wall, "SetAllowAutoJoin"): wall.SetAllowAutoJoin(False)
                w_curve = wall.Location.Curve
                if not (w_curve.GetEndPoint(0).IsAlmostEqualTo(line.GetEndPoint(0)) and \
                        w_curve.GetEndPoint(1).IsAlmostEqualTo(line.GetEndPoint(1))):
                    wall.Location.Curve = line
                    is_changed = True
                reused[0] += 1
            else:
                wall = DB.Wall.Create(doc, line, lvl.Id, False)
                if hasattr(wall, "SetAllowAutoJoin"): wall.SetAllowAutoJoin(False)
                safe_set_comment(wall, ai_id)
                created[0] += 1
                is_changed = True
            
            if is_changed:
                affected_elements.append(wall)
            
            h = w_data.get("height")
            if h:
                p = wall.get_Parameter(DB.BuiltInParameter.WALL_USER_HEIGHT_PARAM)
                if p: p.Set(mm_to_ft(h))
            
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
        import Autodesk.Revit.DB as DB # type: ignore
        doc = self.doc
        # Grids and Sections (Placeholder for full implementation in doc worker)
        pass

    def _expand_lifts_in_manifest(self, manifest, current_levels, elevations, floor_dims, affected_elements=[]):
        """Calculates needed lifts using efficiency/load factors and expands into modular cores.
        Returns core_bounds in FT for column culling."""
        import Autodesk.Revit.DB as DB # type: ignore
        lifts_config = manifest.get("lifts", {})
        shell = manifest.get("shell", {})
        num_storeys = len(current_levels) - 1
        
        # Rule: Auto-trigger if explicitly asked OR if building is 3+ storeys
        if not lifts_config and not shell.get("include_lifts") and num_storeys < 3:
            return None

        # 1. Load Presets for Efficiency & Load Factor
        presets = load_presets()
        setup = manifest.get("project_setup", {})
        typology = setup.get("typology", "commercial_office").lower().replace(" ", "_")
        preset = presets.get(typology, presets.get("commercial_office", {}))
        
        efficiency = preset.get("building_identity", {}).get("target_efficiency", 0.82)
        load_factor = preset.get("program_requirements", {}).get("occupancy_load_factor", 10.0)

        # 2. Calculate Occupancy & Floor Centroid
        total_occ = 0
        min_x, min_y = float('inf'), float('inf')
        max_x, max_y = float('-inf'), float('-inf')
        
        for w, l in floor_dims:
            usable_area = (w * l) * efficiency
            total_occ += (usable_area / 1000000.0) / load_factor
            # Assuming floor_dims are local (centered on 0,0) in RevitWorkers
            # but if they were parsed from real floors, we calculate bounds.
            min_x = min(min_x, -w/2.0); max_x = max(max_x, w/2.0)
            min_y = min(min_y, -l/2.0); max_y = max(max_y, l/2.0)
            
        f_center_x = (min_x + max_x) / 2.0
        f_center_y = (min_y + max_y) / 2.0

        num_lifts = lifts_config.get("count")
        if num_lifts is None or num_lifts == "random":
            avg_h = (elevations[-1] / num_storeys * 304.8) if num_storeys > 0 else 4000
            num_lifts = lift_logic.calculate_lift_requirements(num_storeys, avg_h, total_occ)
        
        # 3. Handle Multi-Block Layout
        num_lifts_val = int(num_lifts)
        lift_size = lifts_config.get("size", preset.get("core_logic", {}).get("lift_shaft_size", [2500, 2500]))
        lobby_w = lifts_config.get("lobby_width", 3000)
        
        layout = lift_logic.get_total_core_layout(num_lifts_val, lift_size, lobby_w)
        num_lifts_val = layout['total_lifts']
        
        # Centering target
        center_pos = lifts_config.get("position")
        if not center_pos:
            center_pos = [f_center_x, f_center_y]
            
        levels_data = []
        for i, lvl in enumerate(current_levels):
            levels_data.append({"id": lvl.Name, "elevation": elevations[i] * 304.8})
            
        lift_walls = []
        remaining_lifts = num_lifts_val
        for b_idx in range(layout['num_blocks']):
            b_lifts = min(remaining_lifts, layout['lifts_per_block'])
            remaining_lifts -= b_lifts
            
            # Use centered b_y_offset to keep entire core cluster balanced around building center
            b_y_offset = (b_idx - (layout['num_blocks']-1)/2.0) * layout['block_d']
            b_center_pos = [center_pos[0], center_pos[1] + b_y_offset]
            
            b_manifest = lift_logic.generate_lift_shaft_manifest(
                b_lifts, levels_data, 
                center_pos=b_center_pos,
                internal_size=lift_size,
                lobby_width=lobby_w
            )
            for w in b_manifest.get("walls", []):
                w['id'] = f"{w['id']}_B{b_idx+1}"
                lift_walls.append(w)
            
            for f in b_manifest.get("floors", []):
                f['id'] = f"{f['id']}_B{b_idx+1}"
                # Inject into manifest["floors"] for processing
                if "floors" not in manifest: manifest["floors"] = []
                manifest["floors"].append(f)
        
        # 4. Inject into manifest for granular processing
        if "walls" not in manifest: manifest["walls"] = []
        manifest["walls"].extend(lift_walls)
        
        # Return Core Bounds in FEET for culling logic
        if lift_walls:
            xs = [mm_to_ft(w['start'][0]) for w in lift_walls] + [mm_to_ft(w['end'][0]) for w in lift_walls]
            ys = [mm_to_ft(w['start'][1]) for w in lift_walls] + [mm_to_ft(w['end'][1]) for w in lift_walls]
            return (min(xs), min(ys), max(xs), max(ys))
        return None

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
        import Autodesk.Revit.DB.Architecture as Arch # type: ignore
        
        base_lvl_id = DB.ElementId(int(state.get(data['base_level_id'])))
        top_lvl_id = DB.ElementId(int(state.get(data['top_level_id'])))
        loc = DB.XYZ(mm_to_ft(data['x']), mm_to_ft(data['y']), 0)
        
        scope = Arch.StairsEditScope(self.doc, "BIM: Stair")
        stair_id = scope.Start(base_lvl_id, top_lvl_id)
        t = DB.Transaction(self.doc, "Stair Run")
        t.Start()
        try:
            # Simple straight run for MVP, U-shape logic is complex but we can approximate
            p1 = loc
            p2 = loc + DB.XYZ(mm_to_ft(3000), 0, 0)
            line = DB.Line.CreateBound(p1, p2)
            Arch.StairsRun.CreateStraightRun(self.doc, stair_id, line, Arch.StairsRunJustification.Center)
            t.Commit()
        except Exception as e:
            t.RollBack()
            raise e
        scope.Commit(Arch.StairsFailureHandlingOptions())
        return [{"stair_id": str(stair_id.Value)}]

    def generate_service_core(self, data, state):
        """Worker for Core Generation"""
        import Autodesk.Revit.DB as DB # type: ignore
        pts = data['boundary_points']
        doc = self.doc
        
        t = DB.Transaction(doc, "BIM: Service Core")
        t.Start()
        try:
            # 1. Create reinforced concrete walls
            wt = DB.FilteredElementCollector(doc).OfClass(DB.WallType).FirstElement()
            lvl = doc.ActiveView.GenLevel
            curve_loop = DB.CurveLoop()
            
            for i in range(len(pts)):
                p1 = DB.XYZ(mm_to_ft(pts[i]['x']), mm_to_ft(pts[i]['y']), 0)
                p2 = DB.XYZ(mm_to_ft(pts[(i+1)%len(pts)]['x']), mm_to_ft(pts[(i+1)%len(pts)]['y']), 0)
                line = DB.Line.CreateBound(p1, p2)
                DB.Wall.Create(doc, line, wt.Id, lvl.Id, mm_to_ft(20000), 0, False, False)
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
            
            # 1. Targeted Join: Walls with Floors
            # Only join walls/floors that were part of the current build action
            for f in floors:
                if not f.IsValidObject: continue
                bb = f.get_BoundingBox(None)
                if not bb: continue
                outline = DB.Outline(bb.Min - DB.XYZ(0.01, 0.01, 1.0), bb.Max + DB.XYZ(0.01, 0.01, 1.0))
                filter = DB.BoundingBoxIntersectsFilter(outline)
                
                # We limit the join candidates to AI walls only
                intersecting_walls = DB.FilteredElementCollector(doc, ids_list).OfClass(DB.Wall).WherePasses(filter).ToElements()
                for w in intersecting_walls:
                    try:
                        if not DB.JoinGeometryUtils.AreElementsJoined(doc, w, f):
                            DB.JoinGeometryUtils.JoinGeometry(doc, w, f)
                    except: pass
            
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
