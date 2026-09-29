"""Second source review of the authored ten; append a corrected v2, never freeze."""
import argparse
import copy
import csv
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rag_baseline.benchmark.core import (
    CONTENT_CHECKS, REVIEW_CHECKS, Corpus, digest, duplicate_clusters, export_review,
    file_hash, import_review, now, read_jsonl, similarity, validate_item, write_json, write_jsonl,
)
from rag_baseline.benchmark.quality import content_fingerprint, evidence_audit, named_document

SOURCE = ROOT / 'data/benchmark/batch_002_authored10'
AUTH = '再帮我审核一遍这新增的十题'
REVIEWER = 'Codex（用户委托第二轮原文复审；同一agent，非独立人工复审）'
FINDINGS = {
    1: ('retain', '无须改动。第57条完整列出事件补救、部门和个人通知、三项通知内容；不通知例外仅针对个人，保留部门要求通知的权力。',
        ['batch_002_64-0013', 'batch_001-0023'], '旧题考常规预防措施或重要数据风险报告，本题考个人信息事件发生或可能发生后的通知内容与豁免。'),
    2: ('needs_revision', '原题未明确合法处理依据及成年主体，scope_note中的限制没有出现在题面；原评分把单独同意写成无条件要求。第13条第2款明确存在不需取得同意的基础。修订题面为以个人同意为依据处理成年客户信息，评分同步限定该前提。',
        ['trial_v3-pilot_v2-0013', 'batch_002_64-0013'], '旧题分别考AI陪伴交互数据及未成年人保护、一般安全措施；本题考成年敏感信息的处理门槛、同意形式和额外告知。'),
    3: ('needs_revision', '原题只据消保法第25条，但题面泛问网购无理由退货的确认情形和商品状态；固定语料实施条例第19条另有不得扩大排除范围、显著确认及不得默认同意、合理拆封调试和诚信退货规则，原答案不完整。补充该条并同步完整题面和评分，改cross_document。',
        ['batch_001-0013', 'batch_001_supplement_01-0001', 'batch_002_64-0049'], '旧题考质量缺陷行政调查、调解期限或鉴定机构；本题考网购无理由退货的权利条件与履行规则。'),
    4: ('minor_correction', '答案和主体对应有原文支持，题目是在列举机构义务及禁止行为，没有要求条件判断；answer_form去掉condition，保留fact。',
        ['batch_001-0033', 'batch_002_64-0045'], '旧题考探索性研究免责或产学研合作，本题考科研机构的伦理审查、诚信管理和禁止服务。'),
    5: ('retain', '无须改动。正文列出的通信技术与认证技术范围完整，保留等字的非穷尽性质；不是附录中标准发布状态的现实判断。',
        ['batch_001-0048', 'batch_002_64-0012'], '旧题考总体基础及终端设施标准类别、国际合作；本题考网联通信安全的通信技术和认证技术范围。'),
    6: ('retain', '无须改动。配置清单审计、重大变更计划及影响分析与实施前测试、补丁跟踪升级及安装前评估测试，完整对应配置和补丁管理三项。未添加安装后步骤或时限。',
        ['batch_001-0038', 'batch_002_64-0064', 'batch_001-0024'], '旧题考身份认证、远程访问或事件处置，本题考配置与补丁管理。'),
    7: ('retain', '无须改动。8.1的完整性和关联核验及反馈、8.2的两类数据转换绑定与保存关联完整；接收端转换和发送端打标是不同任务。与旧静态打标题局部相似，但本题核心包含接收核验、转换及关联维护，不能用旧答案替代。',
        ['batch_001-0008', 'batch_002_64-0038', 'batch_001-0050'], '旧题是静态初始打标、发送端生成传输和周期设置；本题是接收端核验到转换存储的完整流程。'),
    8: ('minor_correction', '原答案没有事实错误；但防回滚目标与低版本无法升级的结果语义重复，按两个必要独立证据单元设置中等题不够稳健。收窄为专门测评步骤和预期结果，删除重复目标评分，使用完整6.3.4单元，改single_unit/easy。',
        ['batch_001-0018', 'batch_002_64-0036'], '防回滚版本测试区别于安全启动、度量验证及安全存储单元测试。'),
    9: ('minor_correction', '原答案支持性成立，但提高实时性和可靠性的概括点与前后原因重复，且题面已给10ms前提。重组为协议互通、实时计算原因、本地存活三项，不重复给概括结论或题面阈值计分，也不保证任意边缘部署达到10ms。',
        ['batch_001-0028', 'batch_001-0044', 'batch_002_64-0048'], '旧题考架构演进、工厂网络改进和数字孪生，本题考边缘计算在协议、时延和本地存活方面的作用。'),
    10: ('minor_correction', '原答案应/宜/可及多云条件准确。题面一般文件可能被误解为一般数据，版本控制原文适用于云上对象存储并不限于一般数据；改为云上对象存储的文件，保留重要及核心数据对象锁条件。',
        ['batch_002_64-0004', 'batch_002_64-0050', 'batch_001-0022'], '旧题考平台加密、数据库日志和平台存储计算服务，本题考对象锁、版本控制、异地及多云灾备。'),
}


def build(corpus, originals):
    items = copy.deepcopy(originals)
    def bundle(term, bids):
        docs = [d for d in corpus.documents.values() if term in Path(d['source_path']).name]
        assert len(docs) == 1
        did = docs[0]['doc_id']
        return [corpus.evidence(did, bid, corpus.blocks[did,bid]['text']) for bid in bids]

    i = items[1]
    i['user_input'] = '企业拟以个人同意为处理依据，处理成年客户的医疗健康和金融账户信息。这些信息属于哪一类？处理的目的、必要性、保护措施、同意形式以及额外告知分别有哪些要求？额外告知规则保留了什么例外？'
    i['answer_points'][2] = '在题设以个人同意为处理依据的情形下，处理敏感个人信息应取得个人的单独同意'
    i['scope_note'] = '题面明确成年客户和以个人同意为处理依据，仅问第28—30条的敏感信息类别、处理门槛、同意形式及额外告知；不把单独同意外推到第13条规定的不需取得同意情形，不考未成年人处理。'

    i = items[2]
    i['user_input'] = '消费者网购商品后申请无理由退货，退货期限从何时起算，哪些商品或确认情形不适用？经营者对不适用商品的范围、提示及购买确认有什么限制？商品完好如何判断，退款期限和退货运费如何规定，消费者行使退货权应遵循什么要求？'
    old_points = originals[2]['answer_points']
    pairs = [(p,['g1']) for p in old_points[:6]] + [
        ('经营者不得擅自扩大不适用无理由退货的商品范围',['g2']),
        ('应显著标注不适用无理由退货的商品并提示消费者在购买时确认，不得设置为消费者默认同意的选项',['g2']),
        ('未经消费者确认，经营者不得拒绝无理由退货',['g2']),
        (old_points[6],['g1']),
        ('为查验打开包装，或为确认品质和功能作合理调试，且不影响原有品质、功能和外观的，经营者应予退货',['g2']),
        (old_points[7],['g1']), (old_points[8],['g1']),
        ('消费者无理由退货应遵循诚实信用，不得利用退货规则损害经营者和其他消费者合法权益',['g2']),
    ]
    i['answer_points'] = [p for p,_ in pairs]
    i['point_evidence'] = [m for _,m in pairs]
    i['evidence_groups'].append(dict(id='g2',alternatives=[bundle('消费者权益保护法实施条例_', [32,33,34,35])]))
    i['evidence_span'] = 'cross_document'
    i['multi_source_reason'] = '消保法第25条给期限、四类排除及其他确认情形、退款和运费；实施条例第19条补充经营者确认程序、合理拆封调试仍应退货和诚信要求。两份文档各含不可被另一份替代的评分信息，围绕同一无理由退货任务。'
    i['scope_note'] = '限定固定消保法第25条及实施条例第19条的网购无理由退货，包含例外、确认、完好判断和诚信，不问质量缺陷救济、行政调解或售后修理期限。'
    i['difficulty_reason'] = '需合并两份文档对同一退货权利的期限、例外与实施条件，并区分收货和收到退回商品两个七日起点。'

    items[3]['answer_form'] = ['fact']

    i = items[7]
    i['user_input'] = '测评人工智能加速芯片的固件防回滚功能时，应怎样构建测试固件、执行操作，并期待什么结果？'
    i['answer_points'] = originals[7]['answer_points'][1:]
    i['evidence_groups'] = [dict(id='g1',alternatives=[bundle('加速芯片安全',[192,193,194,195])])]
    i['point_evidence'] = [['g1'] for _ in i['answer_points']]
    i.update(answer_form=['procedure'], evidence_span='single_unit',difficulty='easy',multi_source_reason='',
        scope_note='只考6.3.4专门防回滚测评的构造、操作及预期结果，不再重复评分防回退目标；不考安全启动或升级来源校验。',
        difficulty_reason='从单一完整防回滚测评小节提取测试固件、操作与预期结果，无需合并多个必要证据单元。')

    i = items[8]
    i['answer_points'] = [
        '协议互通：提供现场级计算能力，实现不同制式网络通信协议相互转换和互联互通，降低工业现场复杂性',
        '实时性：在靠近工业现场的网络边缘侧就近运行处理和分析，提高实时性；分析和控制逻辑全部放在云端难以满足题设部分工控场景的时延要求',
        '持续运行：需要具备不受网络传输带宽和负载影响的本地存活能力，以避免断网、时延过大等影响实时性生产，边缘计算用于提高这一可靠性',
    ]
    i['point_evidence'] = [['g1'] for _ in i['answer_points']]
    i['scope_note'] = '只解释协议互通、实时性和本地存活三个指定维度；10ms前提不单独计分，不保证任何边缘部署都能达到10ms；同一连续跨页段落仍为single_unit。'

    i = items[9]
    i['user_input'] = '在云上对象存储的数据保护与备份中，重要数据和核心数据怎样防止有效期内被覆写或删除？云上对象存储应开启什么功能防止文件意外覆盖或删除？异地容灾和多云灾备分别有哪些建议及适用条件？'
    i['scope_note'] = '只问固定数据库联网安全要求的规范性附录A.3；版本控制适用于云上对象存储的文件，不限于一般数据，保留对象锁的重要/核心数据条件及应/宜/可和多云灾备适用条件。'

    for n,(item,old) in enumerate(zip(items,originals),1):
        if content_fingerprint(item) != content_fingerprint(old):
            item['reference'] = '；'.join(item['answer_points'])+'。'
        item['query_style'] = 'document' if named_document(item['user_input'],corpus) else 'natural'
        validate_item(item,corpus)
    return items


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='data/benchmark/batch_002_authored10/review_v2')
    parser.add_argument('--check-only',action='store_true')
    args = parser.parse_args()
    output = (ROOT/args.output).resolve()
    if not output.is_relative_to((ROOT/'data/benchmark').resolve()):
        raise ValueError('Output must be inside data/benchmark')
    if output.exists():
        raise FileExistsError(output)
    corpus = Corpus(ROOT/'data/processed/baseline_v1')
    original = read_jsonl(SOURCE/'approved.jsonl')
    assert len(original) == 10
    old_pool = read_jsonl(SOURCE/'pool150.jsonl')
    assert old_pool[140:] == original
    anchors = old_pool[:140]
    prior_manifest = json.loads((SOURCE/'manifest.json').read_text(encoding='utf-8'))
    protected = dict(prior_manifest['protected_artifact_hashes'])
    old_artifacts = json.loads((SOURCE/'artifact_hashes.json').read_text(encoding='utf-8'))
    for name,expected in old_artifacts.items():
        assert file_hash(SOURCE/name)==expected,name
    for p in SOURCE.rglob('*'):
        if p.is_file():protected[str(p.relative_to(ROOT))] = file_hash(p)
    for name,expected in protected.items():assert file_hash(ROOT/name)==expected,name
    items = build(corpus,original)
    changed = [n for n,(i,o) in enumerate(zip(items,original),1) if content_fingerprint(i)!=content_fingerprint(o)]
    assert changed == [2,3,4,8,9,10]
    for i in original:validate_item(i,corpus,approved=True)
    identifiers = {i['id'] for i in items}
    _,all_links = duplicate_clusters(anchors+items)
    links = [pair for pair in all_links if identifiers.intersection(pair)]
    assert not links,links
    pool = anchors+items
    assert len(pool)==len({i['id'] for i in pool})==150
    assert Counter(i['category'] for i in pool)=={'policy':75,'technical':75}
    raw_hashes = {}
    for item in items:
        for g in item['evidence_groups']:
            for a in g['alternatives']:
                for e in a:
                    filename = ROOT/'data/raw'/e['source_path']
                    if str(filename) not in raw_hashes:raw_hashes[str(filename)]=file_hash(filename)
                    assert raw_hashes[str(filename)]==e['document_sha256'],filename
    if args.check_only:
        print(json.dumps(dict(total=10,retained=4,changed=6,initial_decisions=dict(Counter(f[0] for f in FINDINGS.values())),
            duplicate_links=links,raw_sources_hash_verified=len(raw_hashes),
            evidence_span=dict(Counter(i['evidence_span'] for i in items))),ensure_ascii=False,indent=2))
        return
    output.mkdir(parents=True)
    revisions=[]
    audits=[]
    final_candidates=[]
    content_fields=['user_input','reference','answer_points','answer_form','evidence_span','evidence_groups',
        'point_evidence','difficulty','difficulty_reason','scope_note','multi_source_reason','query_style']
    for n,(item,old) in enumerate(zip(items,original),1):
        decision,finding,related,duplicate_reason=FINDINGS[n]
        fields=[k for k in content_fields if item.get(k)!=old.get(k)]
        audits.append(dict(id=item['id'],ordinal=n,source_item_sha256=digest(old),
            before_decision=decision,finding=finding,final_review_status='approved',
            related_reference_ids=related,duplicate_reason=duplicate_reason,
            point_support=[dict(ordinal=k+1,point=p,groups=m,supported=True)
                for k,(p,m) in enumerate(zip(item['answer_points'],item['point_evidence']))],
            evidence_audit=evidence_audit(item,corpus),
            completeness='已逐题核对连续条款、补充实施规则、条件和例外；同一概括结论不重复计分。',
            source_quality='以固定解析文本审阅；原始引用文件SHA256与证据文档哈希一致。正文非导航或参考文献，引用区间逐字校验。本轮未声称新增PDF视觉检查。',
            review_method='same_agent_second_source_review',independent_reviewer=False))
        if fields:
            revisions.append(dict(id=item['id'],ordinal=n,reason=finding,changed_fields=fields,
                changes={k:dict(before=old.get(k),after=item.get(k)) for k in fields},
                source_item_sha256=digest(old),before_content_sha256=content_fingerprint(old),
                after_content_sha256=content_fingerprint(item)))
        item['historical_review_provenance'] = dict(review_provenance=old.get('review_provenance'),
            approval_provenance=old.get('approval_provenance'),reviewer=old['reviewer'],
            review_notes=old['review_notes'],original_candidate_hash=old.get('original_candidate_hash'),
            source_batch=str(SOURCE.relative_to(ROOT)),source_item_sha256=digest(old))
        for k in ['semantic_precheck','boundary_precheck','machine_notes','review_provenance',
                  'approval_provenance','reviewed_at','original_candidate_hash']:
            item.pop(k,None)
        item.update(review_status='pending',reviewer='',review_notes='',review_checks={},
            revision_note=finding,source_id=old['id'],source_candidate_sha256=digest(old),
            second_review_provenance=dict(authorization=AUTH,method='same_agent_second_source_review',
                source_batch=str(SOURCE.relative_to(ROOT)),independent_reviewer=False,
                human_per_item_review_claimed=False))
        item['evidence_audit'] = evidence_audit(item,corpus)
        final_candidates.append(item)
    write_jsonl(output/'candidates.jsonl',final_candidates)
    export_review(final_candidates,output/'review',corpus)
    sheet=output/'review/review.csv'
    with sheet.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f);fields=reader.fieldnames;rows=list(reader)
    for row,audit in zip(rows,audits):
        checks={k:True for k in (*REVIEW_CHECKS,*CONTENT_CHECKS)}
        checks['corpus_search']=False
        row.update(review_status='approved',reviewer=REVIEWER,
            review_notes=audit['finding']+' 修订后已复核通过；同一agent第二轮审核，未冻结。',
            review_checks=json.dumps(checks,ensure_ascii=False))
    with sheet.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fields);writer.writeheader();writer.writerows(rows)
    reviewed=import_review(final_candidates,sheet,corpus)
    assert [content_fingerprint(i) for i in reviewed]==[content_fingerprint(i) for i in items]
    for item,audit in zip(reviewed,audits):
        provenance=dict(method='user_requested_second_source_review',authorization=AUTH,
            reviewer=REVIEWER,independent_reviewer=False,human_per_item_review_claimed=False,audit_file='audit.jsonl')
        item['review_provenance']=provenance
        item['approval_provenance']={**provenance,'at':item['reviewed_at']}
        validate_item(item,corpus,approved=True)
        audit['final_item_sha256']=digest(item)
    write_jsonl(output/'approved.jsonl',reviewed)
    write_jsonl(output/'reviewed.jsonl',reviewed)
    write_jsonl(output/'audit.jsonl',audits)
    write_jsonl(output/'revisions.jsonl',revisions)
    export_review(reviewed,output/'review_after_import',corpus)
    pool=anchors+reviewed
    for item in pool:validate_item(item,corpus,approved=item.get('review_status')=='approved')
    write_jsonl(output/'pool150.jsonl',pool)
    assert read_jsonl(output/'pool150.jsonl')[:140]==anchors
    export_review(pool,output/'pool150_review',corpus)
    extra_context=[]
    for term,bids in [('个人信息保护法_',[36,37,38,39,40,41,42,43,44,45,57,58]),
                      ('消费者权益保护法实施条例_',[32,33,34,35]),
                      ('数据库联网安全要求',[164,165,166,167])]:
        doc=next(d for d in corpus.documents.values() if term in Path(d['source_path']).name)
        extra_context.append(dict(reason='复审额外核对同意依据、退货实施条件及附录身份',
            source_path=doc['source_path'],blocks=[dict(block_id=b,text=corpus.blocks[doc['doc_id'],b]['text']) for b in bids]))
    write_json(output/'source_context.json',extra_context)
    write_json(output/'duplicate_relations.json',dict(reference_count=140,new_count=10,
        new_automatic_links=links,semantic_review=[dict(id=a['id'],related_reference_ids=a['related_reference_ids'],reason=a['duplicate_reason']) for a in audits],
        note='逐题以领域相关旧题核查核心任务；标签接收转换与旧静态打标题有局部概念重叠，但完整核心任务不同。'))
    summary=dict(status='second_review_complete_no_freeze',total=10,unchanged_content_count=4,
        revised_content_count=6,initial_decisions=dict(Counter(f[0] for f in FINDINGS.values())),
        final_status=dict(Counter(i['review_status'] for i in reviewed)),rejected=0,
        category=dict(Counter(i['category'] for i in reviewed)),
        evidence_span=dict(Counter(i['evidence_span'] for i in reviewed)),
        difficulty=dict(Counter(i['difficulty'] for i in reviewed)),
        pool_count=150,pool_category=dict(Counter(i['category'] for i in pool)),
        frozen_gold_count=86,no_freeze_performed=True,original140_objects_unchanged=True,
        validation=dict(csv_import_round_trip=True,schema_and_source_intervals=True,
            raw_sources_hash_verified=len(raw_hashes),new_duplicate_links=0,
            protected_files_unchanged=len(protected)),
        authorization=AUTH,independent_reviewer=False,human_per_item_review_claimed=False,
        ragas_used=False,external_llm_calls=0)
    for name,expected in protected.items():assert file_hash(ROOT/name)==expected,name
    assert not any(k=='ragas' or k.startswith('ragas.') for k in sys.modules)
    write_json(output/'summary.json',summary)
    shutil.copyfile(SOURCE/'families.json',output/'families.json')
    write_json(output/'manifest.json',dict(schema_version=2,status=summary['status'],created_at=now(),
        corpus=corpus.identity(),source_batch=str(SOURCE.relative_to(ROOT)),
        source_candidates_sha256=file_hash(SOURCE/'approved.jsonl'),authorization=AUTH,
        protected_artifact_hashes=protected,script_sha256=file_hash(__file__),
        review_method='same_agent_second_source_review',independent_reviewer=False,no_freeze_performed=True))
    lines=['# 新增10题第二轮审核','',
        '**4题原内容保留，2题实质修订，4题标签/评分/措辞优化；修订后10题均approved，无排除。**','',
        '授权：'+AUTH,'',
        '本轮为同一Codex agent重新对照固定原文的第二轮审核，不是独立人工复审。未使用ragas或外部模型。',
        '首版全部文件、已有86+54题、原始文档及固定语料保持原样；本版未冻结，金标准仍86题。','',
        '## 最新入口','',
        '- `review_after_import/reading.md`：复审后的10题题面、答案、评分点和逐字证据。',
        '- `audit.jsonl`：10题逐题发现、逐点评分支持和针对性去重结论。',
        '- `revisions.jsonl`：6题逐字段前后差异。',
        '- `approved.jsonl` / `reviewed.jsonl`：复审通过的10题。',
        '- `pool150.jsonl` / `pool150_review/reading.md`：用本版10题替换旧合并池末10题，原140对象不变。',
        '- `review/review.csv`：本版已填审核表，绑定本版candidates；后续编辑需新路径导入。',
        '- `source_context.json`：本次额外核对的同意例外、退货实施规则、附录身份。','',
        '## 逐题结论','', '|序号|首版结论|处理及理由|','|---|---|---|']
    labels={'retain':'保留','needs_revision':'需实质修订','minor_correction':'优化'}
    lines += [f'|{n:04d}|{labels[d]}|{finding}|' for n,(d,finding,_,_) in FINDINGS.items()]
    lines += ['', '## 校验与范围','',
        '10题与150题池通过本地schema、逐字证据区间和评分映射校验。审核CSV实际导入再导出，内容指纹一致。',
        f'引用的{len(raw_hashes)}份原始文件SHA256与证据一致；{len(protected)}个历史及语料文件哈希保持不变。',
        '去重除自动规则外，核对了每题对应领域的已有140题。新题无自动重复链；第7题与旧静态打标题的局部概念相同，但接收核验、转换、存储关联的整体任务不同。',
        '本轮以固定解析正文审核，未新增PDF视觉检查。3题within_document、1题cross_document、6题single_unit；不存在强行按页数拆分证据。',
        '合计政策75题、技术75题。保留12道源pending及其agent_review.approved，未改旧审核记录或划分dev/holdout。','',
        '## 重现','', 'conda rag环境下使用尚不存在的输出路径；--check-only不写输出。','', '```powershell',
        'python scripts/review_benchmark_authored10.py --output data/benchmark/batch_002_authored10/review_v2_replay',
        '```']
    (output/'report.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    shutil.copyfile(__file__,output/'assemble_review.py')
    write_json(output/'artifact_hashes.json',{str(p.relative_to(output)):file_hash(p) for p in sorted(output.rglob('*')) if p.is_file()})
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
