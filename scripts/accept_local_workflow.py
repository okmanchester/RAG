"""Bounded real PDF/DOCX HTTP + browser acceptance in disposable MySQL/ES namespaces.

Uses installed Chrome and the real local models. All evidence stays below tmp/.
No parser/encoder test doubles. --worker only observes DeepSeek HTTP counts.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag.management.config import Settings
from rag.management.storage import file_hash
from rag.ops.state import atomic_json


def observed_worker(output):
    import httpx
    from unittest.mock import patch
    from rag.management.service import ManagementService
    from rag.management.worker import run_worker
    from rag.ops.events import start_logging
    start_logging("worker")
    counters = {"requests":0,"successful_responses":0}
    original = httpx.AsyncClient.send
    async def send(client, request, *args, **kwargs):
        selected = request.url.host == "api.deepseek.com"
        if selected:
            counters["requests"] += 1
            atomic_json(output, counters)
        response = await original(client, request, *args, **kwargs)
        if selected and response.is_success:
            counters["successful_responses"] += 1
            atomic_json(output, counters)
        return response
    atomic_json(output,counters)
    settings=Settings.from_env()
    engine=settings.engine()
    try:
        with patch.object(httpx.AsyncClient,"send",send):
            run_worker(ManagementService(engine,settings.storage_root))
    finally:
        engine.dispose()


def wait_for(fn, *, seconds=240, description="condition"):
    deadline=time.monotonic()+seconds
    last=None
    while time.monotonic()<deadline:
        try:
            last=fn()
            if last:
                return last
        except (ConnectionError, OSError):
            pass
        time.sleep(.25)
    raise AssertionError("timed out: "+description)


def acceptance(output,port):
    import httpx
    from sqlalchemy import select,update,text
    from playwright.sync_api import sync_playwright,expect
    from rag.management.models import ProcessingTask,IndexSyncState,DocumentVersion,DocumentBuild,Document,Chunk
    from rag.management.service import ManagementService
    from rag.ops.process import Child
    from rag.search.config import SearchSettings
    from rag.search.corpus import ManagedCorpus
    from scripts.backup_knowledge_base import container_sql,restore,backup
    from unittest.mock import patch

    output=output.resolve()
    if not output.is_relative_to(ROOT / "tmp") or output.exists():
        raise ValueError("choose a new output directory below tmp/")
    output.mkdir(parents=True)
    initial=Settings.from_env()
    database="rag_accept_"+uuid.uuid4().hex[:12]
    restored=database+"_restore"
    storage=output / "storage"
    report={"database":database,"started_at":datetime.now().isoformat(),"checks":[],"samples":[],"browser_errors":[]}
    environment={**os.environ,"MYSQL_DATABASE":database,"RAG_STORAGE_ROOT":str(storage),
                 "RAG_RUNTIME_ROOT":str(output / "runtime"),"RAG_INDEX_PREFIX":"rag-accept"}
    api_child=worker=None
    es=SearchSettings.from_env().client()
    settings=replace(initial,database=database,storage_root=storage)
    engine=settings.engine()
    service=ManagementService(engine,storage)
    namespace=ManagedCorpus(engine).namespace
    base=f"http://127.0.0.1:{port}"
    client=httpx.Client(base_url=base,timeout=120,trust_env=False)
    counters=[]

    def check(name):
        report["checks"].append(name)
        atomic_json(output / "report.json",report)
        print(name,flush=True)

    def get(path):
        response=client.get("/api/v1"+path)
        response.raise_for_status()
        return response.json()

    def post(path,body=None):
        response=client.post("/api/v1"+path,json=body or {})
        response.raise_for_status()
        return response.json()

    def start_worker():
        target=output / f"deepseek-{len(counters)}.json"
        counters.append(target)
        return Child([sys.executable,str(Path(__file__)),"--worker",str(target)],cwd=ROOT,env=environment)

    def task_done(task_id):
        task=get(f"/processing-tasks/{task_id}")
        if task["status"]=="failed":
            raise AssertionError("real processing failed: "+str(task["error_code"]))
        return task if task["status"]=="succeeded" else None

    def synced(kb):
        value=get(f"/knowledge-bases/{kb}/index-status")
        if value["sync_task"]["status"]=="failed":
            raise AssertionError("sync failed: "+str(value["sync_task"]["error_code"]))
        if value["sync_task"]["status"]!="succeeded":
            return None
        value=get(f"/knowledge-bases/{kb}/index-status?db_only=false")
        return value if value["sync_lag"] is False else None

    def activate(doc,build):
        response=client.put(f"/api/v1/documents/{doc}/active-build",json={"build_id":build})
        response.raise_for_status()
        return response.json()

    try:
        container_sql(ROOT / ".env.mysql",f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci; GRANT ALL PRIVILEGES ON `{database}`.* TO '{initial.user}'@'%';")
        service.db_upgrade()
        kb=service.kb_create("acceptance","真实文档验收")["id"]
        api_child=Child([sys.executable,"-m","uvicorn","rag.api.app:create_app","--factory","--host","127.0.0.1","--port",str(port),"--no-access-log"],cwd=ROOT,env=environment)
        def reachable():
            try:return client.get("/health").status_code==200
            except httpx.HTTPError:return False
        wait_for(reachable,seconds=30,description="HTTP startup")
        check("isolated HTTP startup and current migration")
        pdf=next((ROOT / "data/raw/policy").glob("工业和信息化部关于印发*2026*通知*.pdf"))
        docx=ROOT / "data/raw/policy/中华人民共和国数据安全法_20210610.docx"
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(channel="chrome",headless=True)
            page=browser.new_page(viewport={"width":1440,"height":1050},locale="zh-CN")
            page.on("pageerror",lambda e:report["browser_errors"].append(str(e)))
            page.on("dialog",lambda dialog:dialog.accept())
            page.goto(base)
            expect(page.locator("#kb")).to_have_value(str(kb))
            uploads=[]
            for source in (pdf,docx):
                path="acceptance/"+source.name
                page.locator("#source-path").fill(path)
                page.locator("#file").set_input_files(str(source))
                with page.expect_response(lambda r:r.request.method=="POST" and r.url.endswith(f"/knowledge-bases/{kb}/documents"),timeout=120000) as result:
                    page.locator("#upload-button").click()
                response=result.value
                assert response.status==202,response.text()
                item=response.json()
                item.update(source=str(source.relative_to(ROOT)),source_sha256=file_hash(source))
                uploads.append(item)
                expect(page.locator("#upload-result")).to_contain_text("上传完成")
            assert all(get(f"/documents/{x['document_id']}")["active_build_id"] is None for x in uploads)
            check("browser uploads real PDF and DOCX; both queued, neither activated")
            worker=start_worker()
            first=uploads[0]
            wait_for(lambda:get(f"/processing-tasks/{first['task_id']}")["stage"]=="parsing",description="real PDF parsing")
            worker.stop()
            assert get(f"/processing-tasks/{first['task_id']}")["status"]=="running"
            # Isolated fault injection advances only the expired lease; no business data is changed.
            with engine.begin() as connection:
                connection.execute(update(ProcessingTask).where(ProcessingTask.id==first["task_id"]).values(lease_expires_at=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(seconds=1)))
            worker=start_worker()
            for item in uploads:
                started=time.monotonic()
                task=wait_for(lambda:task_done(item["task_id"]),seconds=480,description="real document processing")
                item["build_id"]=task["build_id"]
                item["processing_wait_seconds"]=round(time.monotonic()-started,3)
                if item==first:
                    assert any(a["status"]=="interrupted" for a in task["attempts"]["items"])
                assert get(f"/documents/{item['document_id']}")["active_build_id"] is None
            check("real parsing and chunking; terminated processing tree recovered through lease reclamation (isolated expiry injection)")
            # Browser activation of the DOCX, with worker stopped so sync lag is observable.
            worker.stop()
            second=uploads[1]
            page.reload()
            expect(page.locator("#document-detail")).to_contain_text(docx.stem)
            expect(page.get_by_role("button",name="启用 / 回退")).to_be_enabled()
            page.get_by_role("button",name="启用 / 回退").click()
            wait_for(lambda:get(f"/documents/{second['document_id']}")["active_build_id"]==second["build_id"])
            expect(page.locator("#index")).to_contain_text("等待索引同步",timeout=15000)
            activate(first["document_id"],first["build_id"])
            worker=start_worker()
            wait_for(lambda:get(f"/knowledge-bases/{kb}/index-status")["sync_task"]["status"]=="running",description="real indexing")
            worker.stop()
            with engine.begin() as connection:
                connection.execute(update(IndexSyncState).where(IndexSyncState.knowledge_base_id==kb).values(lease_expires_at=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(seconds=1)))
            worker=start_worker()
            status=wait_for(lambda:synced(kb),seconds=480,description="real BGE indexing")
            assert any(a["status"]=="interrupted" for a in status["attempts"]["items"])
            check("explicit browser activation, visible sync wait, terminated index process recovery and real BGE convergence")
            for item,query in zip(uploads,("人工智能 信息通信 创新发展","数据安全 风险监测")):
                result=post(f"/knowledge-bases/{kb}/search",{"query":query,"mode":"bm25","top_k":10})
                hits=[c for c in result["contexts"] if c["document_id"]==item["document_id"]]
                assert hits and all(c["document_sha256"]==item["source_sha256"] and c["build_id"]==item["build_id"] and c["sources"] for c in hits)
                item["citation_example"]=hits[0]
            report["samples"]=uploads
            page.get_by_role("button",name="检索文档",exact=True).click()
            page.locator("#query").fill("数据安全 风险监测")
            page.locator("#mode").select_option("bm25")
            page.locator("#search-button").click()
            expect(page.locator(".result").first).to_be_visible(timeout=120000)
            expect(page.locator("#search-results")).to_contain_text("内容位置")
            page.locator(".result summary").first.click()
            expect(page.locator(".result pre").first).to_contain_text("SHA256")
            assert not report["browser_errors"],report["browser_errors"]
            page.screenshot(path=str(output / "search.png"),full_page=True)
            check("browser retrieval shows real DOCX positions, citation hashes and build identity")
            for mode in ("dense","hybrid"):
                result=post(f"/knowledge-bases/{kb}/search",{"query":"数据安全风险监测","mode":mode,"top_k":5})
                assert result["contexts"] and not result["sync_lag"]
            # Repeated upload uses the original raw version and completed task.
            with docx.open("rb") as stream:
                duplicate=client.post(f"/api/v1/knowledge-bases/{kb}/documents",data={"source_path":"acceptance/"+docx.name},files={"file":(docx.name,stream)})
            assert duplicate.status_code==200 and duplicate.json()["reused_version"] and duplicate.json()["reused_task"]
            # New valid DOCX bytes under the same logical path; original files remain untouched.
            changed=output / "changed.docx"
            with zipfile.ZipFile(docx) as source,zipfile.ZipFile(changed,"w",zipfile.ZIP_DEFLATED) as target:
                for member in source.infolist():
                    data=source.read(member.filename)
                    if member.filename=="word/document.xml":
                        extra='<w:p><w:r><w:t>验收更新标记：海棠七号数据安全记录。</w:t></w:r></w:p>'
                        data=data.replace(b"</w:body>",extra.encode()+b"</w:body>")
                    target.writestr(member,data)
            with changed.open("rb") as stream:
                response=client.post(f"/api/v1/knowledge-bases/{kb}/documents",data={"source_path":"acceptance/"+docx.name},files={"file":(docx.name,stream)})
            response.raise_for_status()
            new=response.json()
            assert new["document_id"]==second["document_id"] and new["version_id"]!=second["version_id"]
            newtask=wait_for(lambda:task_done(new["task_id"]),seconds=180)
            worker.stop()
            activate(second["document_id"],newtask["build_id"])
            lag=post(f"/knowledge-bases/{kb}/search",{"query":"数据安全","mode":"bm25"})
            assert lag["sync_lag"] and not any(c["build_id"]==second["build_id"] for c in lag["contexts"])
            worker=start_worker()
            wait_for(lambda:synced(kb))
            assert any(c["build_id"]==newtask["build_id"] for c in post(f"/knowledge-bases/{kb}/search",{"query":"海棠七号","mode":"bm25"})["contexts"])
            activate(second["document_id"],second["build_id"])
            response=client.patch(f"/api/v1/documents/{second['document_id']}/status",json={"status":"disabled"})
            response.raise_for_status()
            assert not any(c["document_id"]==second["document_id"] for c in post(f"/knowledge-bases/{kb}/search",{"query":"数据安全","mode":"bm25"})["contexts"])
            client.patch(f"/api/v1/documents/{second['document_id']}/status",json={"status":"active"}).raise_for_status()
            wait_for(lambda:synced(kb))
            check("dense/hybrid, deduplication, same-path update, old-build filtering, rollback and immediate disable")
            page.get_by_role("button",name="文档与版本",exact=True).click()
            page.locator("#source-path").fill("broken.pdf")
            page.locator("#file").set_input_files({"name":"broken.pdf","mimeType":"application/pdf","buffer":b"not a pdf"})
            with page.expect_response(lambda r:r.request.method=="POST" and r.url.endswith(f"/knowledge-bases/{kb}/documents")) as result:
                page.locator("#upload-button").click()
            broken=result.value.json()
            wait_for(lambda:get(f"/processing-tasks/{broken['task_id']}")["status"]=="failed",seconds=120)
            page.get_by_role("button",name="处理任务",exact=True).click()
            expect(page.locator("#task-list")).to_contain_text("PROCESSING_FAILED",timeout=15000)
            page.screenshot(path=str(output / "failure.png"),full_page=True)
            check("real malformed PDF produces persisted failure and readable browser recovery action")
            # Controlled UI fault only: actual pipeline success above remains independently recorded.
            page.route("**/api/v1/knowledge-bases/*/search",lambda route:route.fulfill(status=503,json={"error_code":"SEARCH_UNAVAILABLE","error":"search unavailable"}))
            page.get_by_role("button",name="检索文档",exact=True).click()
            page.locator("#search-button").click()
            expect(page.locator("#search-results")).to_contain_text("SEARCH_UNAVAILABLE")
            expect(page.locator("#query")).to_have_value("数据安全 风险监测")
            page.unroute("**/api/v1/knowledge-bases/*/search")
            page.get_by_role("button",name="文档与版本",exact=True).click()
            page.locator("#source-path").fill("empty.pdf")
            page.locator("#file").set_input_files({"name":"empty.pdf","mimeType":"application/pdf","buffer":b""})
            page.locator("#upload-button").click()
            expect(page.locator("#upload-result")).to_contain_text("EMPTY_UPLOAD")
            page.locator("#source-path").fill("bad.txt")
            page.locator("#file").set_input_files({"name":"bad.txt","mimeType":"text/plain","buffer":b"hello"})
            page.locator("#upload-button").click()
            expect(page.locator("#upload-result")).to_contain_text("INVALID_DOCUMENT_FORMAT")
            page.set_viewport_size({"width":760,"height":1000})
            page.screenshot(path=str(output / "mobile.png"),full_page=True)
            check("browser error injection preserves query, rejects empty/unsupported files; responsive screenshot")
            browser.close()
        # Online backup while the HTTP service and worker remain alive.
        with patch.dict(os.environ,environment):
            backup(ROOT / ".env.mysql",output / "backup")
            restore(ROOT / ".env.mysql",output / "backup",restored,output / "restored-storage")
        restored_engine=replace(settings,database=restored,storage_root=output / "restored-storage").engine()
        try:
            with restored_engine.connect() as connection:
                for row in connection.execute(select(DocumentVersion)).mappings():
                    assert file_hash(Path(row["storage_uri"]))==row["source_sha256"]
                from rag.management.storage import contained
                for row in connection.execute(select(DocumentBuild)).mappings():
                    manifest=row["artifact_manifest_json"]
                    if isinstance(manifest,str):manifest=json.loads(manifest)
                    for name,digest in manifest["files_sha256"].items():
                        assert file_hash(contained(Path(manifest["snapshot_uri"]),name))==digest
            ManagementService(restored_engine,output / "restored-storage").db_check()
        finally:restored_engine.dispose()
        assert client.get("/health").status_code==200
        check("online backup and isolated restore verify every referenced raw file and build asset; source API remains available")
        report["status"]="passed"
    except BaseException as exc:
        report["status"]="failed"
        report["failure_type"]=type(exc).__name__
        raise
    finally:
        if worker:worker.stop()
        if api_child:api_child.stop()
        client.close()
        report["deepseek"]={key:sum(json.loads(p.read_text()).get(key,0) for p in counters if p.exists()) for key in ("requests","successful_responses")}
        pattern=f"rag-accept-kb-{namespace}-*"
        for name in es.indices.get_mapping(index=pattern,allow_no_indices=True):
            if not name.startswith(pattern[:-1]):raise RuntimeError("namespace mismatch")
            es.indices.delete(index=name)
        es.close()
        engine.dispose()
        for name in (restored,database):
            if not name.startswith("rag_accept_"):raise RuntimeError("database mismatch")
            container_sql(ROOT / ".env.mysql",f"DROP DATABASE IF EXISTS `{name}`;")
        report["finished_at"]=datetime.now().isoformat()
        atomic_json(output / "report.json",report)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path)
    parser.add_argument("--port",type=int,default=18080)
    parser.add_argument("--worker",type=Path)
    args=parser.parse_args()
    if args.worker:
        observed_worker(args.worker)
    elif args.output:
        acceptance(args.output,args.port)
    else:parser.error("--output is required")


if __name__=="__main__":
    main()
