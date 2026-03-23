# -*- coding: utf-8 -*-
import json

class QAChecker:
    def __init__(self, work_order):
        self.work_order = work_order
        self.errors = []

    def validate_hosting(self):
        """Check if apertures have valid wall hosts in the work order"""
        shell = self.work_order.get('arch_shell', {})
        walls = {w.get('id') for w in shell.get('walls', [])}
        skin = self.work_order.get('arch_skin', {})
        
        for aperture in skin.get('apertures', []):
            host_id = aperture.get('host_id')
            if host_id not in walls:
                self.errors.append({
                    "agent": "Agent 4 (Skin)",
                    "error": "Floating Element",
                    "details": "Aperture {} references non-existent Wall ID {}".format(aperture.get('id'), host_id),
                    "correction": "Host the aperture on a valid Wall ID from Agent 3's output."
                })

    def validate_clashes(self):
        """Soft clash check: Columns vs Doors"""
        # Simplified: Check coordinate overlap if provided in JSON
        struct = self.work_order.get('structural_plan', {})
        skin = self.work_order.get('arch_skin', {})
        
        for col in struct.get('columns', []):
            cx, cy = col.get('x'), col.get('y')
            for aperture in skin.get('apertures', []):
                # Placeholder for complex bounding box logic
                # For now, flag if they are exactly at the same point (unlikely in grid, but possible error)
                if aperture.get('offset') == 0: # simplified check
                    pass 

    def run_all_checks(self):
        self.validate_hosting()
        self.validate_clashes()
        return self.errors

def perform_qa(work_order):
    checker = QAChecker(work_order)
    return checker.run_all_checks()
