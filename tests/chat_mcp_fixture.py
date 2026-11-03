"""Real local MCP protocol fixture; no network or filesystem mutation."""
from mcp.server.fastmcp import FastMCP
server=FastMCP('chat-test')
@server.tool()
def echo(text:str)->str:
    return 'actual MCP response: '+text
if __name__=='__main__':server.run(transport='stdio')
