import asyncio
import json
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client

async def test_revit_mcp():
    url = "http://localhost:8001/sse"
    print(f"Connecting to {url}...")
    
    try:
        async with sse_client(url) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                print("Session initialized.")
                
                # Initialize
                await session.initialize()
                print("Initialized!")

                # List Tools
                tools = await session.list_tools()
                print("\nAvailable Tools:")
                for tool in tools.tools:
                    print(f"- {tool.name}: {tool.description}")

                # Call a tool
                print("\nCalling 'query_elements'...")
                result = await session.call_tool("query_elements", arguments={"category_name": "Walls"})
                print("Result:")
                print(result.content[0].text)

                # List Resources
                resources = await session.list_resources()
                print("\nAvailable Resources:")
                for res in resources.resources:
                    print(f"- {res.uri}: {res.name}")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_revit_mcp())
