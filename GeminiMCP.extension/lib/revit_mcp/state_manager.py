# -*- coding: utf-8 -*-

class StateManager:
    def __init__(self):
        self._state = {
            "mappings": {}, # User-defined ID to Revit ElementId conversion
            "elements": {}, # Detailed info of created elements
            "work_order": {} # The current master plan
        }

    def update_mapping(self, logical_id, element_id):
        self._state["mappings"][logical_id] = str(element_id)

    def get_element_id(self, logical_id):
        return self._state["mappings"].get(logical_id)

    def set_work_order(self, order):
        self._state["work_order"] = order

    def get_state(self):
        return self._state

state_manager = StateManager()
