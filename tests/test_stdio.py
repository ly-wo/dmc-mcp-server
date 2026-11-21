"""Exercise the actual MCP process without production credentials or network."""
import asyncio
import sys
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class StdioTests(unittest.TestCase):
    def test_tools_and_rejection_over_stdio(self):
        async def check():
            params = StdioServerParameters(
                command=sys.executable, args=['-m', 'dmc_mcp_server.main'],
                env={'DMC_CREDENTIALS_FILE': '/nonexistent/dmc-offline-test.json'},
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual(len(tools.tools), 8)
                    self.assertNotIn('set_cookie', {tool.name for tool in tools.tools})
                    result = await session.call_tool('execute_select', {
                        'instance_id': 'fixture', 'db_name': 'db',
                        'sql': 'SELECT 1; DELETE FROM fixture',
                    })
                    self.assertTrue(result.isError)
                    self.assertIn('Exactly one SQL statement', result.content[0].text)
        asyncio.run(asyncio.wait_for(check(), timeout=15))
