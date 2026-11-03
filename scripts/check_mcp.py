import asyncio,json,sys
from pathlib import Path
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
async def main():
    port=sys.argv[1] if len(sys.argv)>1 else '7861'
    token=httpx.get(f'http://127.0.0.1:{port}/api/v1/settings',trust_env=False).json()['mcp_token']
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+token},trust_env=False) as transport:
        async with streamable_http_client(f'http://127.0.0.1:{port}/mcp/',http_client=transport) as (r,w,_):
            async with ClientSession(r,w) as client:
                init=await client.initialize();tools=await client.list_tools();result=await client.call_tool('douyin_auth_status',{})
                record={'server':init.serverInfo.model_dump(),'tool_count':len(tools.tools),'auth':json.loads(result.content[0].text)}
                print(json.dumps(record,ensure_ascii=False));Path('D:/素材采集/studio/mcp-report.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':asyncio.run(main())
