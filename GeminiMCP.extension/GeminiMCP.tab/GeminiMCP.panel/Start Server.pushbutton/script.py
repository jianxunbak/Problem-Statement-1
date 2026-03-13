#! python3
# -*- coding: utf-8 -*-
import os
import sys

# Ensure the lib folder is accessible
lib_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'lib'))
if lib_path not in sys.path:
    sys.path.append(lib_path)

from revit_mcp.runner import start_mcp_server

def main():
    print("Initializing Gemini FastMCP Server...")
    start_mcp_server()
    print("Server startup requested.")

if __name__ == '__main__':
    main()
