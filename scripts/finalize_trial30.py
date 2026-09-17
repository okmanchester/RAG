"""One-time, user-delegated sign-off for the specifically reviewed trial_v3 snapshot."""
from __future__ import annotations

import copy
import csv
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rag_baseline.benchmark.core import (
    Corpus, REVIEW_CHECKS, V2_EDITABLE, duplicate_clusters, export_review, file_hash,
    freeze_trial, import_review, load_release, now, read_jsonl, validate_item, write_json, write_jsonl,
)
from rag_baseline.benchmark.quality import CONTENT_CHECKS, compact, content_fingerprint, evidence_audit

REVIEWER = 'Codex（受用户明确委托完成审核）'
AUTHORIZATION = '替我把需要人工审核填写的内容填上，然后把该做的准备工作做好，就把这30题彻底定稿'
EXPECTED_CANDIDATES = '62ad3b398cbd70f0f39fab7a212c83f80857d1b8957adce157d3c6091ca54227'
EXPECTED_REVIEW = '4a3966cc9f143383d803d243352b7aa32acb70e626d8c03c35a60c3ebb7d6dac'


def source_search(corpus, terms):
    results = []
    for term in terms:
        matches = [{'doc_id':d['doc_id'], 'document_sha256':d['sha256'], 'source_path':d['source_path'],
                    'block_id':b['block_id'], 'text':b['text']}
                   for d in corpus.documents.values() for b in d['blocks'] if compact(term) in compact(b['text'])]
        results.append({'query':term, 'hit_count':len(matches), 'matches':matches})
    return {'method':'all_document_blocks_nfkc_whitespace_normalized_literal_search',
            'document_count':len(corpus.documents), 'corpus':corpus.identity(), 'queries':results,
            'limitation':'No-hit is not proof about reality; reviewed against the fixed corpus and relevant provisions only.'}


def main():
    source = Path('data/benchmark/trial_v3')
    output = Path('data/benchmark/trial30_final_v1')
    candidate_path, csv_path = source/'candidates.jsonl', source/'review/review.csv'
    if output.exists() or (source/'reviewed.jsonl').exists():
        raise FileExistsError('This sign-off is append-only and has already been prepared')
    if file_hash(candidate_path) != EXPECTED_CANDIDATES or file_hash(csv_path) != EXPECTED_REVIEW:
        raise ValueError('The reviewed snapshot changed; this authorization must not approve new content blindly')
    corpus = Corpus('data/processed/baseline_v1')
    manifest = json.loads((source/'manifest.json').read_text(encoding='utf-8'))
    if corpus.identity() != manifest['corpus']:
        raise ValueError('Corpus snapshot changed')
    protected = {path:sha for path,sha in manifest['protected_files'].items()}
    if any(file_hash(path) != sha for path,sha in protected.items()):
        raise ValueError('Knowledge base or historical source batch changed')
    candidates = read_jsonl(candidate_path)
    submitted = import_review(candidates, csv_path, corpus)
    groups, links = duplicate_clusters(submitted)
    if links or any(len(g)>1 for g in groups):
        raise ValueError('Duplicate question needs resolution before sign-off')
    boundary_search = {
        'abstain':source_search(corpus,['云桥','数据交易服务有限公司','数据交易中介']),
        'clarify':source_search(corpus,['委托处理个人信息','向其他个人信息处理者提供','单独同意']),
    }
    if any(q['hit_count'] for q in boundary_search['abstain']['queries'][:2]):
        raise ValueError('New counterevidence found for the synthetic individual-case question')
    audit_rows, approvals = [], {}
    for item in submitted:
        audit = evidence_audit(item, corpus)
        if audit['issues']:
            raise ValueError(f"Unresolved content issues: {item['id']}")
        flags = sorted({flag for record in audit['records'] for flag in record['flags']})
        behavior = item['expected_behavior']
        boundary_note = ''
        if behavior == 'abstain':
            boundary_note = '全33篇固定解析文本实体名称检索0命中；已复核数据交易中介全部命中，仅有一般义务和法定罚则，不能推出合成个案实际金额。不将未命中解释为现实事件不存在。'
        elif behavior == 'clarify':
            boundary_note = '已全库搜索委托处理、向其他处理者提供及单独同意，并核对个人信息保护法第21、23条及上下文；两种处理关系影响义务，先澄清成立，不作无条件同意推断。'
        detail = '已逐题复核问题范围、必要要点及对应原文；限定条件、例外与案例范围已保留；标签及难度与内容相符。已结合全组题目比较核心答案，无待合并的重复题。'
        if item['id'].endswith('0002') and item['expected_behavior']=='answer':
            detail += '跨页表格已对照原PDF第76–77页复核，表头及VLAN/VxLAN列关系正确；不把4096视为无保留可分配数量。'
        if 'heading_only' in flags:
            detail += '标题仅用于场景身份锚定，具体监测和告警结论由正文支持。'
        detail += boundary_note
        checks = {k: True for k in (*REVIEW_CHECKS,*CONTENT_CHECKS)}
        checks['corpus_search'] = behavior != 'answer'
        notes = '[历史审核意见]\n' + item.get('review_notes','') + '\n[用户委托定稿审核]\n' + detail
        approvals[item['id']] = {'review_status':'approved','reviewer':REVIEWER,'review_checks':checks,'review_notes':notes}
        audit_rows.append({'id':item['id'],'content_sha256':content_fingerprint(item),'reviewer':REVIEWER,
            'review_method':'agent_delegated','human_per_item_review_claimed':False,'authorization':AUTHORIZATION,
            'result':'approved','scope':item['scope_note'],'point_evidence':item['point_evidence'],
            'difficulty_reason':item['difficulty_reason'],'flags_reviewed':flags,'rationale':detail,
            'corpus_search_applicable':behavior!='answer',
            'corpus_search':boundary_search.get(behavior), 'evidence_audit':audit})

    # First validate all completed decisions in memory, before changing the working CSV.
    for item in submitted:
        validate_item({**item, **approvals[item['id']]}, corpus, approved=True)
    prior_csv = source/'review/review_before_delegated_signoff.csv'
    if prior_csv.exists():
        raise FileExistsError(prior_csv)
    shutil.copyfile(csv_path, prior_csv)
    with csv_path.open(encoding='utf-8-sig',newline='') as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        csv_rows = list(reader)
    for row in csv_rows:
        for k,v in approvals[row['id']].items():
            row[k] = json.dumps(v,ensure_ascii=False) if k=='review_checks' else v
    temporary = csv_path.with_suffix('.signoff.tmp')
    with temporary.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fields);writer.writeheader();writer.writerows(csv_rows)
    temporary.replace(csv_path)
    reviewed = import_review(candidates, csv_path, corpus)
    audit_by_id = {a['id']:a for a in audit_rows}
    for row in reviewed:
        row['approval_provenance'] = {'method':'agent_delegated','reviewer':REVIEWER,'authorization':AUTHORIZATION,
                                    'human_per_item_review_claimed':False,'at':now()}
        row['finalization_audit'] = audit_by_id[row['id']]
        if row['expected_behavior']!='answer':
            row['corpus_search_review'] = boundary_search[row['expected_behavior']]
        validate_item(row,corpus,approved=True)
    write_jsonl(source/'reviewed.jsonl',reviewed)
    write_jsonl(source/'finalization_audit.jsonl',audit_rows)

    families=json.loads((source/'families.json').read_text(encoding='utf-8'))
    families.update(review_status='approved',reviewer=REVIEWER,review_method='agent_delegated',
        notes='已核对33篇文档身份及重复发布关系；保留生成式人工智能暂行办法两个发布版本同族。信息通信实施意见全文与仅310字符通知不误合并；算法推荐和深度合成两个来源独立。本30题试审集不要求覆盖全部文档族。')
    write_json(source/'families_reviewed.json',families)
    reviewed_manifest={**manifest,'status':'reviewed_trial','reviewer':REVIEWER,
        'review_method':'agent_delegated','approval_authorization':AUTHORIZATION,
        'reviewed_sha256':file_hash(source/'reviewed.jsonl'),
        'review_csv_sha256':file_hash(csv_path),'prior_review_csv_sha256':file_hash(prior_csv),
        'finalization_audit_sha256':file_hash(source/'finalization_audit.jsonl'),
        'implementation_sha256':{str(p):file_hash(p) for p in [Path(__file__),*Path('rag_baseline/benchmark').glob('*.py')]}}
    write_json(source/'reviewed_manifest.json',reviewed_manifest)
    # Synchronize reading/audit with filled fields, retaining original candidate hashes in the working CSV.
    with tempfile.TemporaryDirectory() as td:
        export_review(reviewed,td,corpus)
        for name in ('reading.md','evidence_audit.jsonl'):
            shutil.copyfile(Path(td)/name,source/'review'/name)
    freeze_trial(reviewed,corpus,families,output,reviewed_manifest)
    gold, release = load_release(output)
    assert len(gold)==30 and all(i['split']=='dev' and i['review_status']=='approved' for i in gold)
    assert all(file_hash(path)==sha for path,sha in protected.items())
    print(json.dumps({'release':str(output),'approved':len(gold),'split':'dev','reviewer':REVIEWER,
                      'protected_files_unchanged':True},ensure_ascii=False))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
