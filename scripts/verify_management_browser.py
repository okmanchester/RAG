"""Read-only browser checks against the installed UI, plus controlled response faults."""
import argparse
import asyncio
import copy
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


async def verify(base,output):
    import httpx
    from playwright.async_api import async_playwright,expect
    output=output.resolve()
    if not output.is_relative_to(ROOT / "tmp"):
        raise ValueError("output must be below tmp")
    output.mkdir(parents=True,exist_ok=True)
    async with httpx.AsyncClient(base_url=base,trust_env=False,timeout=30) as client:
        page_data=(await client.get('/api/v1/knowledge-bases')).json()
        kb=page_data['items'][0]['id']
        index=(await client.get(f'/api/v1/knowledge-bases/{kb}/index-status')).json()
        search=(await client.post(f'/api/v1/knowledge-bases/{kb}/search',json={'query':'数据安全','mode':'bm25','top_k':1})).json()
    checks=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel='chrome',headless=True)
        page=await browser.new_page(viewport={'width':1440,'height':1000},locale='zh-CN')
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        await page.goto(base)
        await expect(page.locator('#document-list button.link').first).to_be_visible()
        await page.locator('#document-list button.link').first.click()
        summary=page.locator('#document-detail summary').first
        await expect(summary).to_be_visible()
        await summary.click()
        await page.wait_for_timeout(2600)
        assert await page.locator('#document-detail details[open]').count()==1
        title=await page.locator('#document-detail h2').inner_text()
        await page.reload()
        await expect(page.locator('#document-detail h2')).to_have_text(title)
        checks.append('polling preserves expanded SHA details; reload restores selected document')
        await page.screenshot(path=str(output / 'documents.png'))
        # Plain-text rendering must not interpret hostile source text as HTML.
        assert search.get('contexts'),search
        injected=copy.deepcopy(search)
        injected['contexts'][0]['text']='<img src=x onerror="document.body.dataset.injected=1">原文证据'
        await page.route('**/api/v1/knowledge-bases/*/search',lambda route:route.fulfill(json=injected))
        await page.get_by_role('button',name='检索文档',exact=True).click()
        await page.locator('#query').fill('引用渲染测试')
        await page.locator('#search-button').click()
        await expect(page.locator('.result').first).to_contain_text('<img src=x')
        assert await page.locator('.result img').count()==0
        assert await page.locator('body').get_attribute('data-injected') is None
        checks.append('source text is rendered literally, with no executable HTML')
        await page.unroute('**/api/v1/knowledge-bases/*/search')
        # An old search must not populate the newly selected KB.
        virtual=copy.deepcopy(page_data['items'][0]);virtual.update(id=999999,name='隔离浏览器场景')
        await page.route('**/api/v1/knowledge-bases?*',lambda route:route.fulfill(json={**page_data,'total':len(page_data['items'])+1,'items':[*page_data['items'],virtual]}))
        empty={'items':[],'total':0,'offset':0,'limit':10}
        virtual_index=copy.deepcopy(index);virtual_index['kb_id']=999999
        virtual_index['sync_task']['knowledge_base_id']=999999
        await page.route('**/api/v1/knowledge-bases/999999/index-status*',lambda route:route.fulfill(json=virtual_index))
        await page.route('**/api/v1/knowledge-bases/999999/documents?*',lambda route:route.fulfill(json=empty))
        await page.reload()
        await expect(page.locator('#kb option')).to_have_count(len(page_data['items'])+1)
        async def delayed(route):
            await asyncio.sleep(.8)
            try:await route.fulfill(json=search)
            except Exception:pass  # Browser cancellation is the expected outcome.
        await page.route('**/api/v1/knowledge-bases/*/search',delayed)
        await page.locator('#query').fill('旧知识库请求')
        await page.locator('#search-button').click()
        await page.locator('#kb').select_option('999999')
        await page.wait_for_timeout(1400)
        assert await page.locator('.result').count()==0
        checks.append('changing knowledge base cancels or discards old search responses')
        # Runtime errors are observations, not task failure inference.
        unknown={'signal':'stale','updated_at':None,'supervisor':'unknown','api':{'state':'unknown'},'worker':{'state':'unknown'},'dependencies':{},'backup':None}
        await page.route('**/api/v1/runtime-status',lambda route:route.fulfill(json=unknown))
        await page.locator('#refresh').click()
        await expect(page.locator('#runtime')).to_contain_text('监督信号已过期')
        checks.append('stale supervisor signal displays uncertainty')
        # A failed initial KB load can recover with the existing refresh control.
        await page.unroute('**/api/v1/knowledge-bases?*')
        await page.route('**/api/v1/knowledge-bases?*',lambda route:route.fulfill(status=503,json={'error_code':'DATABASE_ERROR','error':'database unavailable'}))
        await page.reload()
        await expect(page.locator('#connection')).to_contain_text('DATABASE_ERROR')
        await page.unroute('**/api/v1/knowledge-bases?*')
        await page.locator('#refresh').click()
        await expect(page.locator('#kb')).to_have_value(str(kb))
        checks.append('initial connection failure recovers without a page reload')
        await page.set_viewport_size({'width':760,'height':1000})
        await page.screenshot(path=str(output / 'responsive.png'),full_page=True)
        assert not errors,errors
        await browser.close()
    (output / 'report.json').write_text(json.dumps({'status':'passed','checks':checks,'browser_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':'passed','checks':checks},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:8000')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    asyncio.run(verify(args.base_url,args.output))
