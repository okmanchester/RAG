"""Author ten fixed-corpus questions without Ragas or external model calls.

Append-only output; preserve gold86 and reviewed54. Run in conda rag.
"""
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
    Corpus, REVIEW_CHECKS, digest, duplicate_clusters, export_review, file_hash,
    import_review, now, read_jsonl, similarity, validate_item, write_json, write_jsonl,
)
from rag_baseline.benchmark.quality import CONTENT_CHECKS, content_fingerprint, evidence_audit, named_document

AUTH = '请你不使用ragas，根据现在已有的86题金标准，和已审核过的新增的54题，自行根据文档内容补齐剩下的10题'
REVIEWER = 'Codex（用户委托原文编写及自检；非独立人工复审）'
BASE = ROOT / 'data/benchmark'
INPUTS = [
    BASE / 'trial30_final_v1/gold.jsonl',
    BASE / 'batch56_final_v1/gold.jsonl',
    BASE / 'batch_002_review64/agent_review_v1/suggested_approved.jsonl',
    BASE / 'batch_002_review64/revision_v2/approved.jsonl',
]


def author(corpus):
    items = []

    def evidence(term, ids):
        docs = [d for d in corpus.documents.values() if term in Path(d['source_path']).name]
        if len(docs) != 1:
            raise ValueError(f'Ambiguous source: {term}')
        did = docs[0]['doc_id']
        return [corpus.evidence(did, bid, corpus.blocks[did, bid]['text']) for bid in ids]

    def add(category, question, points, sources, forms, scope, note,
            span='single_unit', difficulty='easy', mapping=None, multi=''):
        groups = [{'id': f'g{n+1}', 'alternatives': [evidence(term, bids)]}
                  for n, (term, bids) in enumerate(sources)]
        if mapping is None:
            assert len(groups) == 1
            mapping = [['g1'] for _ in points]
        item = dict(
            id=f'batch_002_authored10-{len(items)+1:04d}', schema_version=2,
            category=category, user_input=question, reference='；'.join(points)+'。',
            answer_points=points, evidence_groups=groups, point_evidence=mapping,
            answer_form=forms, evidence_span=span, difficulty=difficulty,
            difficulty_reason=('需结合不同条款或技术要求与测评方法，完成同一具体任务。'
                               if span == 'within_document' else
                               '需按指定情境整理完整证据单元中的条件、例外或原因。'
                               if difficulty == 'medium' else
                               '从一个完整条款、连续枚举或连续说明段落直接提取答案。'),
            query_style='document' if named_document(question, corpus) else 'natural',
            scope_note=scope, multi_source_reason=multi, expected_behavior='answer',
            boundary_reason='', clarifying_interpretations=[], corpus_search=[],
            review_status='pending', reviewer='', review_checks={}, review_notes='',
            duplicate_group='', created_at=now(), authoring_note=note,
            generation_provenance=dict(method='codex_source_grounded_authoring',
                author='Codex', authorization=AUTH, ragas_used=False,
                external_llm_calls=0, human_per_item_review_claimed=False,
                reference_gold_count=86, reference_reviewed_count=54),
        )
        item['evidence_audit'] = evidence_audit(item, corpus)
        validate_item(item, corpus)
        items.append(item)

    add('policy',
        '发生或者可能发生个人信息泄露、篡改、丢失时，处理者应立即采取什么措施、通知谁，通知须包含什么内容？在什么条件下可以不通知个人，保护职责部门仍有什么权力？',
        ['应立即采取补救措施', '应通知履行个人信息保护职责的部门和个人',
         '通知应包括发生或可能发生泄露、篡改、丢失的信息种类、原因及可能造成的危害',
         '通知应包括处理者采取的补救措施及个人可以采取的减轻危害的措施',
         '通知应包括个人信息处理者的联系方式',
         '采取措施能够有效避免造成危害的，可以不通知个人；该例外不免除通知保护职责部门的义务',
         '保护职责部门认为可能造成危害的，有权要求处理者通知个人'],
        [('个人信息保护法_', list(range(141,146)))], ['procedure','condition'],
        '只问个人信息保护法第57条事件补救与通知，不问未规定的小时数或报告模板；不通知例外只针对个人。',
        '核对完整法条三项通知内容和末段例外；与旧题重要数据风险报告、网络应急演练属于不同信息对象和任务。',
        difficulty='medium')
    add('policy',
        '企业拟处理客户的医疗健康和金融账户信息。按个人信息保护规则，这些信息属于哪一类？处理的目的、必要性、保护措施、同意形式以及额外告知分别有什么要求和例外？',
        ['医疗健康和金融账户信息属于敏感个人信息',
         '只有具有特定目的和充分必要性，并采取严格保护措施时，方可处理',
         '处理敏感个人信息应取得个人的单独同意',
         '法律、行政法规规定应取得书面同意的，按其规定执行',
         '除通常告知事项外，还应告知处理敏感个人信息的必要性及对个人权益的影响',
         '依照法律规定可以不向个人告知的，属于上述额外告知的例外'],
        [('个人信息保护法_', [74,75]), ('个人信息保护法_', [76]), ('个人信息保护法_', [77])],
        ['condition','fact'],
        '限定题中成人客户医疗健康、金融账户信息与第28—30条一般规则，不问所有非同意处理基础或未成年人规则。',
        '从类型及处理条件、同意、额外告知三条分别取证；不把严格保护条件简化为取得同意即可。',
        span='within_document', difficulty='medium', mapping=[['g1'],['g1'],['g2'],['g2'],['g3'],['g3']],
        multi='第28条给信息类别与处理门槛，第29条给同意形式，第30条给额外告知及例外；任何单条都不能回答全部场景要求。')
    add('policy',
        '消费者网购商品后申请无理由退货，退货期限从何时起算？哪些商品或确认情形不适用？商品状态、退款期限及退货运费分别有什么规则？',
        ['自收到商品之日起七日内有权退货，无需说明理由',
         '消费者定作的商品不适用', '鲜活易腐的商品不适用',
         '在线下载或者消费者拆封的音像制品、计算机软件等数字化商品不适用',
         '交付的报纸、期刊不适用',
         '其他根据商品性质并经消费者在购买时确认不宜退货的商品，也不适用',
         '退货商品应当完好', '经营者应自收到退回商品之日起七日内返还已支付的商品价款',
         '退回商品运费由消费者承担，双方另有约定的按约定'],
        [('消费者权益保护法_', list(range(59,66)))], ['condition','procedure'],
        '只问消费者权益保护法第25条网购无理由退货，区别于质量缺陷退货及消费争议行政调解期限。',
        '完整覆盖四类排除、购买时确认、商品完好、两种七日起点及运费约定，不把全部数字商品都排除。',
        difficulty='medium')
    add('policy',
        '科研机构、高校和企业事业单位开展科学技术活动，在科技伦理审查和科研诚信管理方面各须履行什么主体责任？任何组织或个人在科研成果及论文、实验数据和项目材料服务方面有哪些禁止行为？',
        ['应履行科技伦理管理主体责任',
         '应按国家有关规定建立健全科技伦理审查机制，对科学技术活动开展科技伦理审查',
         '企业事业单位和社会组织应履行科研诚信管理主体责任',
         '任何组织和个人不得虚构、伪造科研成果',
         '不得发布、传播虚假科研成果',
         '不得从事学术论文及其实验研究数据、科学技术计划项目申报验收材料等的买卖、代写、代投服务'],
        [('科学技术进步法_', [236,237]), ('科学技术进步法_', [238,239,240])],
        ['fact','condition'],
        '只考组织主体的伦理审查与诚信管理及第104条列明禁止行为，不考国家体系建设清单、行政罚款或探索性研究免责。',
        '按伦理与诚信两条区分主体和义务；题面所列机构作为组织，不把诚信责任限定于个人。',
        span='within_document', difficulty='medium', mapping=[['g1'],['g1'],['g2'],['g2'],['g2'],['g2']],
        multi='第103条规范伦理管理和审查，第104条规范诚信主体责任及禁止服务，两条共同回答机构开展科研活动的两类约束。')
    add('technical',
        '车联网标准体系的网联通信安全部分中，通信安全标准覆盖哪些通信技术的安全防护与检测？身份认证标准主要规范哪些技术要求？',
        ['通信安全覆盖蜂窝车联网C-V2X的安全防护与检测',
         '还覆盖应用于车联网的蜂窝移动通信4G/5G、卫星通信、无线射频识别、车内无线局域网、蓝牙低能耗BLE、紫蜂Zigbee及超宽带UWB等',
         '身份认证标准主要规范数字身份认证的证书应用接口',
         '还规范证书管理系统', '还规范安全认证技术及测试方法', '还规范关键部件轻量级认证等技术要求'],
        [('车联网网络安全和数据安全标准体系建设指南', [53,54])], ['fact'],
        '只考网联通信安全类别下的具体规范范围，不考总体基础或终端设施分类、不把规划标准当作已经发布的产品配置要求。',
        '与batch_001-0048的总体基础及终端设施分类不同，新增通信技术覆盖及身份认证技术范围。')
    add('technical',
        '工业控制系统在日常配置管理、重大配置变更和安全补丁更新方面，分别应完成哪些管理、评估及测试工作？',
        ['做好工业控制网络、工业主机和工业控制设备的安全配置',
         '建立工业控制系统配置清单，并定期进行配置审计',
         '重大配置变更应制定变更计划并进行影响分析',
         '配置变更实施前应进行严格安全测试',
         '密切关注重大工控安全漏洞及补丁发布，及时采取补丁升级措施',
         '补丁安装前需进行严格安全评估和测试验证'],
        [('工业控制系统信息安全防护', [19,20,21])], ['procedure'],
        '限定配置和补丁管理完整三项措施，不扩展成全部工控防护；不编造补丁安装完成后的额外流程或安装时限。',
        '检查配置变更与补丁安装各自的事前评估测试条件，区别于既有身份认证、远程访问及事件应急题。')
    add('technical',
        '接收方收到动态数据标签后，应怎样核验和反馈？核验无误后，对非结构化或半结构化数据、结构化数据分别怎样转换和绑定标签，最后应怎样保存数据与标签的关联？',
        ['应对动态标签进行完整性校验，并与数据对象进行关联一致性核验',
         '应将核验结果反馈给发送方', '核验无误后应先把动态标签转换成静态标签',
         '非结构化或半结构化数据应提取数据源主体、数据发送方、数据分级等属性生成静态标签，并完成嵌入式绑定',
         '结构化数据应提取上述属性生成静态标签，并在静态标签映射表中增加或修改映射信息',
         '完成转换后应存储收到的数据对象和生成的静态标签，并维护两者关联'],
        [('标签标识技术要求', [246]), ('标签标识技术要求', [248,249,250,251])],
        ['procedure','comparison'],
        '只考普通动态标签接收核验与转换，非协作平台标识核验；不同于发送方生成、静态初始打标与周期字段设置。',
        '保留先核验后转换存储的顺序，接收方提取的属性原文为数据发送方，不擅自改成数据提供方。',
        span='within_document', difficulty='medium', mapping=[['g1'],['g1']]+[['g2']]*4,
        multi='8.1给完整性及关联核验和反馈，8.2给核验无误后的结构分支转换、存储及关联维护，两节不可互相替代。')
    add('technical',
        '人工智能加速芯片的固件防回滚应实现什么安全要求？验证这项功能时，应构建什么测试固件、执行什么操作，并期待怎样的结果？',
        ['应提供固件防回滚机制，防止固件版本回退',
         '构建版本号低于当前版本的固件', '尝试用该低版本固件升级芯片',
         '预期低版本固件无法在芯片中完成升级'],
        [('加速芯片安全', [92]), ('加速芯片安全', [192,193,194,195])], ['procedure','fact'],
        '只考固件防回滚及专门测试，不等同于安全启动、度量验证或固件来源校验；不要求所有新版本必然能升级。',
        '结合5.3 d)的防回退指标与6.3.4专门测评，避免重复已有安全启动和完整性度量测试。',
        span='within_document', difficulty='medium', mapping=[['g1'],['g2'],['g2'],['g2']],
        multi='安全要求给防止版本回退的目标，专门测评小节给低版本测试构建、操作及预期结果，目标与验证步骤分别来自两处。')
    add('technical',
        '工业现场存在多种总线协议，且部分控制场景计算时延要求在10ms以内。边缘计算为什么适合此类场景？请分别说明它对协议互通、实时性和断网时持续运行的作用。',
        ['在靠近工业现场的网络边缘侧进行处理分析，就近提供计算服务',
         '提供现场级计算能力，支持不同制式网络通信协议相互转换及互联互通，降低工业现场复杂性',
         '部分工控场景要求计算处理时延在10ms以内，分析和控制逻辑全部在云端难以满足实时性',
         '边缘计算提高工业数据计算的实时性和可靠性',
         '需要不受网络传输带宽和负载影响的本地存活能力，以避免断网、时延过大等影响实时性生产'],
        [('体系架构（版本2.0）', [379,380])], ['synthesis'],
        '按固定体系架构的边缘计算技术连续说明段落解释适配原因，10ms仅为部分场景要求，不承诺边缘计算部署一定达标。',
        '10ms前提已在题面给出，评分点考原因与作用，不单独给复述阈值计分。连续跨页段落保持single_unit。',
        difficulty='medium')
    add('technical',
        '在云上对象存储的数据保护与备份中，重要数据和核心数据怎样防止有效期内被覆写或删除？一般文件应开启什么功能防止意外覆盖或删除？异地容灾和多云灾备分别有哪些建议及适用条件？',
        ['重要数据和核心数据应启用对象锁，或确保在设定有效期内存储桶相关数据只读且不可覆写或删除',
         '应开启版本控制功能，防止文件意外覆盖或删除',
         '宜利用存储桶复制功能，将数据复制到其他区域的数据中心，实现异地容灾',
         '对于数据持久性要求严格且数据主要存储在其他云厂商的客户，可采用多云灾备方案'],
        [('数据库联网安全要求', [190,191,193,195,196,197])], ['condition','procedure'],
        '限定云上对象存储附录数据保护与备份，保留应/宜/可及多云灾备条件；不外推为所有数据库强制多云部署。',
        '完整覆盖附录A.3的锁定、版本、备份三项；与已有数据库角色、访问控制、传输及日志留存任务不同。',
        difficulty='medium')
    return items


def coords(item):
    return {(e['doc_id'], e['block_id']) for g in item['evidence_groups']
            for bundle in g['alternatives'] for e in bundle}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='data/benchmark/batch_002_authored10')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    if not output.is_relative_to(BASE.resolve()):
        raise ValueError('Output must stay inside data/benchmark')
    if output.exists():
        raise FileExistsError(output)
    corpus = Corpus(ROOT / 'data/processed/baseline_v1')
    source_sets = [read_jsonl(p) for p in INPUTS]
    assert [len(s) for s in source_sets] == [30,56,12,42]
    gold = source_sets[0]+source_sets[1]
    anchors = gold+source_sets[2]+source_sets[3]
    assert len({i['id'] for i in anchors}) == 140
    assert all(i['agent_review']['agent_review_status']=='approved' for i in source_sets[2])
    assert all(i['review_status']=='approved' for i in source_sets[3])
    rejected = read_jsonl(BASE/'batch_002_review64/agent_review_v1/suggested_rejected.jsonl')
    protected_paths = set(INPUTS)
    for directory in [BASE/'trial30_final_v1', BASE/'batch56_final_v1',
                      BASE/'batch_002_review64', BASE/'batch_002_64', BASE/'batch_002_supplement_01']:
        protected_paths.update(p for p in directory.rglob('*') if p.is_file())
    protected_paths.update(p for p in (ROOT/'data/raw').rglob('*') if p.is_file())
    protected_paths.update(p for p in (ROOT/'data/processed/baseline_v1').rglob('*') if p.is_file())
    protected = {str(p.relative_to(ROOT)):file_hash(p) for p in sorted(protected_paths)}
    candidates = author(corpus)
    assert len(candidates)==10
    assert Counter(i['category'] for i in candidates)=={'policy':4,'technical':6}
    _, links = duplicate_clusters(anchors+candidates)
    candidate_ids = {i['id'] for i in candidates}
    active_links = [pair for pair in links if candidate_ids.intersection(pair)]
    assert not active_links, active_links
    overlaps = []
    audit = []
    for item in candidates:
        ranking = sorted(anchors, key=lambda a: similarity(item['user_input'],a['user_input']), reverse=True)[:3]
        shared = [dict(reference_id=a['id'], shared_blocks=[dict(doc_id=d,block_id=b) for d,b in sorted(coords(item)&coords(a))])
                  for a in anchors if coords(item)&coords(a)]
        overlaps.extend(dict(candidate_id=item['id'], **r) for r in shared)
        audit.append(dict(id=item['id'],question=item['user_input'],self_check_note=item['authoring_note'],
            evidence_audit=evidence_audit(item,corpus),
            duplicate_check=dict(reference_count=140,automatic_links=[],shared_blocks=shared,
                nearest_questions=[dict(id=a['id'],question=a['user_input'],
                    question_similarity=similarity(item['user_input'],a['user_input'])) for a in ranking],
                semantic_decision='已按140题题面及必要答案要点核对，未发现同核心任务；共享文档不等于重复。'),
            independent_reviewer=False,human_per_item_review_claimed=False))
    assert not overlaps, overlaps
    pool = anchors+candidates
    assert Counter(i['category'] for i in pool)=={'policy':75,'technical':75}
    assert not ({i['id'] for i in rejected}&{i['id'] for i in pool})
    if args.check_only:
        print(json.dumps(dict(total=10,pool_count=150,category=dict(Counter(i['category'] for i in pool)),
            new_duplicate_links=active_links,shared_evidence_blocks=overlaps,
            ragas_imported=any(k=='ragas' or k.startswith('ragas.') for k in sys.modules)),ensure_ascii=False,indent=2))
        return
    output.mkdir(parents=True)
    write_jsonl(output/'candidates.jsonl',candidates)
    export_review(candidates,output/'review',corpus)
    csv_path=output/'review/review.csv'
    with csv_path.open(encoding='utf-8-sig',newline='') as f:
        reader=csv.DictReader(f); columns=reader.fieldnames; rows=list(reader)
    for row,item in zip(rows,candidates):
        checks={k:True for k in (*REVIEW_CHECKS,*CONTENT_CHECKS)}
        checks['corpus_search']=False
        row.update(review_status='approved',reviewer=REVIEWER,
            review_checks=json.dumps(checks,ensure_ascii=False),
            review_notes=item['authoring_note']+' 用户委托Codex编写并自检通过，不冒称独立人工复审；未冻结。')
    with csv_path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,columns);writer.writeheader();writer.writerows(rows)
    reviewed=import_review(candidates,csv_path,corpus)
    assert [content_fingerprint(i) for i in reviewed]==[content_fingerprint(i) for i in candidates]
    for item,record in zip(reviewed,audit):
        item['review_provenance']=dict(method='user_requested_source_authoring_self_check',
            authorization=AUTH,reviewer=REVIEWER,independent_reviewer=False,
            human_per_item_review_claimed=False,audit_file='authoring_audit.jsonl')
        item['approval_provenance']={**item['review_provenance'],'at':item['reviewed_at']}
        validate_item(item,corpus,approved=True)
        record['approved_item_sha256']=digest(item)
    write_jsonl(output/'approved.jsonl',reviewed)
    write_jsonl(output/'reviewed.jsonl',reviewed)
    write_jsonl(output/'authoring_audit.jsonl',audit)
    export_review(reviewed,output/'review_after_import',corpus)
    # Read-only assembly: keep all 140 reference objects exactly as received.
    pool=anchors+reviewed
    write_jsonl(output/'pool150.jsonl',pool)
    assert read_jsonl(output/'pool150.jsonl')[:140]==anchors
    export_review(pool,output/'pool150_review',corpus)
    for item in pool:
        validate_item(item,corpus,approved=item.get('review_status')=='approved')
    for name,expected in protected.items():
        assert file_hash(ROOT/name)==expected,name
    assert not any(k=='ragas' or k.startswith('ragas.') for k in sys.modules)
    summary=dict(status='10_source_authored_questions_self_checked_no_freeze',total=10,
        new_category=dict(Counter(i['category'] for i in reviewed)),pool_count=150,
        pool_category=dict(Counter(i['category'] for i in pool)),frozen_gold_count=86,
        reference_reviewed_count=54,reference_original_statuses_preserved=True,
        pool_original_review_status=dict(Counter(i.get('review_status') for i in pool)),
        evidence_span=dict(Counter(i['evidence_span'] for i in reviewed)),
        source_documents=len({d for i in reviewed for d,_ in coords(i)}),
        validation=dict(schema_and_source_intervals=True,point_evidence=True,
            csv_import_round_trip=True,new_duplicate_links=0,shared_evidence_blocks=0,
            semantic_core_self_check=True,protected_files_unchanged=len(protected)),
        ragas_used=False,ragas_imported=False,external_llm_calls=0,
        independent_reviewer=False,human_per_item_review_claimed=False,
        no_freeze_performed=True,no_split_assignment=True,authorization=AUTH)
    write_json(output/'summary.json',summary)
    write_json(output/'duplicate_relations.json',dict(reference_gold_count=86,
        reference_reviewed_count=54,new_count=10,new_links=active_links,shared_blocks=overlaps,
        excluded_original_ids=[i['id'] for i in rejected]))
    shutil.copyfile(BASE/'batch_002_review64/families.json',output/'families.json')
    write_json(output/'manifest.json',dict(schema_version=2,status=summary['status'],created_at=now(),
        corpus=corpus.identity(),source_batches=[dict(path=str(p.relative_to(ROOT)),sha256=file_hash(p),count=len(s))
            for p,s in zip(INPUTS,source_sets)],generation_method='codex_source_grounded_authoring',
        authorization=AUTH,script_sha256=file_hash(__file__),protected_artifact_hashes=protected,
        ragas_used=False,external_llm_calls=0,no_freeze_performed=True))
    lines=['# 原文编写补题10道','',
        '已补政策4题、技术6题；与旧86题及已审核新增54题合计150题，政策、技术各75题。',
        '本批10题已由Codex按固定原文编写并自检approved，没有使用或导入ragas，没有外部模型调用。',
        '授权：'+AUTH,'',
        'approved表示本次委托编写后的同一agent自检，不声称独立人工复审。本轮未冻结，冻结金标准仍86题。',
        '原86及54题原对象、原审核记录、原始文档与固定语料均保留。12道首次审核建议保留题在源数据中仍是pending，',
        '其agent_review状态为approved；pool150忠实保留这两个状态，不将其悄然改为正式approved。',
        '原10道建议排除题没有进入pool150。未设置dev/holdout。','',
        '## 阅读与数据入口','',
        '- `review_after_import/reading.md`：新增10题，含答案、必要要点、原文及来源位置。',
        '- `review/review.csv`：已填写的自检表，绑定本版candidates；修改后需新路径导入与再导出。',
        '- `approved.jsonl`、`reviewed.jsonl`：10道自检通过记录。',
        '- `authoring_audit.jsonl`：逐题选题理由、证据审核、140题去重及最近题面。',
        '- `pool150.jsonl`、`pool150_review/reading.md`：150题合并查看入口，不是新冻结发布。',
        '- `manifest.json`、`summary.json`、`duplicate_relations.json`：来源哈希、校验结果与排除ID。','',
        '## 本批题目','', '|ID|领域|问题|','|---|---|---|']
    lines += [f"|{i['id']}|{i['category']}|{i['user_input']}|" for i in reviewed]
    lines += ['', '## 校验', '',
        '10题及150题合并池通过本地schema、逐字原文区间、文档哈希和评分点映射校验；新10题审核CSV实际导入后再导出。',
        '逐题核对完整枚举、条件例外与应/宜/可。与140题无自动重复链，也无共享证据block；按题面和必要答案另做核心考点自检。',
        '单条款、连续枚举和连续跨页说明保留single_unit，不为配额拆成多跳。4道within_document题由不可替代的独立单元支撑。',
        '所有保护文件已校验字节哈希；未重新解析语料、改动历史、冻结或划分数据集。','',
        '## 重现','', '在conda rag环境运行，输出路径必须尚不存在：','', '```powershell',
        'python scripts/author_benchmark_supplement10.py --output data/benchmark/batch_002_authored10_replay',
        '```','', '`--check-only`仅在内存中构造和校验10题，不写题库。']
    (output/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    shutil.copyfile(__file__,output/'assemble_authoring.py')
    write_json(output/'artifact_hashes.json',{str(p.relative_to(output)):file_hash(p)
        for p in sorted(output.rglob('*')) if p.is_file()})
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
