"""Apply the trial v2 review notes into a new, source-preserving review batch."""
from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rag_baseline.benchmark.core import (
    Corpus, V2_EDITABLE, digest, export_review, file_hash, import_review,
    quotas, read_jsonl, validate_item, write_json, write_jsonl,
)
from rag_baseline.benchmark.quality import CONTENT_CHECKS, evidence_audit
from rag_baseline.benchmark.core import REVIEW_CHECKS


def revise_reviewed(rows, originals, corpus):
    """Keep submitted changes/notes; apply the seven reviewed corrections explicitly."""
    source = {r['id']: r for r in originals}
    result, ledger = [], []
    for submitted in rows:
        row = copy.deepcopy(submitted)
        previous_id = row['id']
        original = source[previous_id]
        suffix = previous_id.removeprefix('trial_v2-pilot_v2-')
        note = '已读取审核意见；题目、答案、证据及标签保持审核表内容。'
        if suffix == '0001':
            row['answer_points'][1] = '查阅、复制当事人与个人信息处理活动有关的合同、记录、账簿以及其他有关资料。'
            note = '采纳措辞建议，补齐查阅、复制资料与当事人及个人信息处理活动的关联限定。'
        elif suffix == '0006':
            row['answer_points'][0] = '考察平台覆盖的特定行业数量及范围。'
            note = '采纳措辞建议，恢复行业覆盖数量维度；仍不询问原文未提供的具体数值门槛。'
        elif suffix == '0010':
            row['user_input'] = row['user_input'].replace('通常定为', '原则上定为')
            note = '采纳措辞建议，题面将“通常”改为“原则上”，与原文限定一致。'
        elif suffix == '0021':
            row['answer_points'][1] = '发展人工智能手机和电脑、智慧家庭设备、智能穿戴设备等产品，培育智能化、融合化人工智能终端产品体系。'
            note = '采纳措辞建议，将“构建”统一为原文的“培育”。'
        elif suffix == '0023':
            row['user_input'] = '因设置恶意程序而被主管部门责令改正、给予警告后，出现什么情况会被罚款？对被处罚主体和直接负责的主管人员，罚款范围分别是多少？'
            row['answer_points'] = [
                '拒不改正或者导致危害网络安全等后果的，会被处以罚款。',
                '对被处罚主体处五万元以上五十万元以下罚款。',
                '对直接负责的主管人员处一万元以上十万元以下罚款。',
            ]
            row['scope_note'] = '已进入所引条款责令改正、警告的处罚情境，只问进一步罚款的条件和两类对象的罚款范围，不判断全部设置恶意程序行为的适用范围。'
            row['difficulty_reason'] = '区分进一步罚款的触发条件，以及被处罚主体和直接负责主管人员的罚款范围。'
            row['point_evidence'] = [['g1'], ['g1'], ['g1']]
            note = '保留审核表删除block 51的修改，仅以119–120作为评分证据；同步收窄问题范围，移除不再有直接证据支持的提供者身份前提。将题面已给出的责令改正、警告移出评分点，改为考查罚款条件及范围。'
        elif suffix == '0025':
            # Preserve the user's exact wording, including the added 政务服务.
            row['scope_note'] = '限定城市基础设施、城乡治理、政务服务和公共资源交易四方面的人工智能应用方向。'
            note = '保留审核表在题面补入“政务服务”的修改；同步明确范围说明，四个答案要点不变。'
        elif suffix == '0026':
            row['user_input'] = row['user_input'].replace('黄埔文冲船舶产业链', '船舶产业链')
            row['scope_note'] = '仅描述资料中的船舶产业链协同优化案例，不泛化到所有工业互联网平台，也不要求识别案例企业名称。'
            note = '保留审核表删除block 408的修改，仅使用415–416同段跨页证据；题面去掉未在剩余引文中出现的企业名称，继续保留船舶案例范围。'
        if suffix in ('0001', '0006', '0021', '0023'):
            row['reference'] = '；'.join(p.rstrip('。；') for p in row['answer_points']) + '。'

        changes_from_submission = [k for k in V2_EDITABLE if k not in ('reviewer','review_notes','review_checks')
                                   and row.get(k) != submitted.get(k)]
        submitted_changes = [k for k in V2_EDITABLE if k not in ('reviewer','review_notes','review_checks')
                             and original.get(k) != submitted.get(k)]
        changed = bool(changes_from_submission or submitted_changes)
        row['id'] = previous_id.replace('trial_v2-', 'trial_v3-', 1)
        row['revision_parent_id'] = previous_id
        row['revision_parent_sha256'] = digest(original)
        row['submitted_review_sha256'] = digest(submitted)
        row['prior_revision_note'] = original.get('revision_note', '')
        row['revision_note'] = note
        row['review_submission'] = {
            'source_id': previous_id, 'review_status': submitted['review_status'],
            'reviewer': submitted.get('reviewer',''), 'review_notes': submitted.get('review_notes',''),
            'review_checks': copy.deepcopy(submitted.get('review_checks',{})),
        }
        if changed:
            row['review_status'] = 'pending'
            row['reviewer'] = ''
            row['review_checks'] = {k: False for k in (*REVIEW_CHECKS, *CONTENT_CHECKS)}
            row.pop('semantic_precheck', None)
            row.pop('boundary_precheck', None)
            row.pop('machine_notes', None)
        validate_item(row, corpus, approved=row['review_status']=='approved')
        row['evidence_audit'] = evidence_audit(row, corpus)
        ledger.append({'id':row['id'], 'source_id':previous_id,
                       'submitted_changes':submitted_changes, 'applied_changes':changes_from_submission,
                       'needs_re_review':changed, 'resolution':note,
                       'original_review_notes':submitted.get('review_notes','')})
        result.append(row)
    return result, ledger


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, default=Path('data/benchmark/trial_v2'))
    p.add_argument('--output', type=Path, default=Path('data/benchmark/trial_v3'))
    p.add_argument('--corpus', type=Path, default=Path('data/processed/baseline_v1'))
    args = p.parse_args()
    if args.output.exists():
        raise FileExistsError('Review history is append-only; choose a new output directory')
    protected = [f for root in (Path('data/raw'),args.corpus,args.source) for f in root.rglob('*') if f.is_file()]
    protected += [f for f in Path('data/benchmark').glob('*graph*') if f.is_file()]
    before = {str(f):file_hash(f) for f in protected}
    corpus = Corpus(args.corpus)
    originals = read_jsonl(args.source/'candidates.jsonl')
    imported = import_review(originals,args.source/'review/review.csv',corpus)
    rows, ledger = revise_reviewed(imported, originals, corpus)
    manifest = json.loads((args.source/'manifest.json').read_text(encoding='utf-8'))
    write_jsonl(args.output/'candidates.jsonl',rows)
    write_jsonl(args.output/'submitted_review.jsonl',imported)
    write_jsonl(args.output/'revisions.jsonl',ledger)
    shutil.copyfile(args.source/'review/review.csv',args.output/'submitted_review.csv')
    shutil.copyfile(args.source/'families.json',args.output/'families.json')
    export_review(rows,args.output/'review',corpus)
    roundtrip = import_review(rows,args.output/'review/review.csv',corpus)
    assert all(all(a.get(k)==b.get(k) for k in V2_EDITABLE) for a,b in zip(rows,roundtrip))
    assert before == {str(f):file_hash(f) for f in protected}
    write_json(args.output/'manifest.json', {
        'schema_version':2, 'status':'candidates_pending_human_review', 'corpus':corpus.identity(),
        'judge':manifest['judge'], 'source_batch':str(args.source), 'source_manifest_sha256':file_hash(args.source/'manifest.json'),
        'source_review_sha256':file_hash(args.source/'review/review.csv'), 'protected_files':before,
        'implementation_sha256':{str(f):file_hash(f) for f in [Path(__file__), *Path('rag_baseline/benchmark').glob('*.py')]},
        'generator':{'kind':'review_feedback_revision','paid_api_calls':0},
    })
    priority = [r['id'] for r in ledger if r['needs_re_review']]
    write_json(args.output/'summary.json',{'candidate_count':len(rows),'priority_re_review':priority,
        'approved':sum(r['review_status']=='approved' for r in rows),'quotas':quotas(rows),
        'csv_round_trip_verified':True,'protected_files_unchanged':True,'protected_file_count':len(before)})
    lines=['# 人工意见修订 v3','','审核入口：review/reading.md（阅读）与 review/review.csv（填写）。',
           '本轮保留提交的审核原文和直接修改；没有自动勾选人工确认或标记通过。',
           '优先复审 0001、0006、0010、0021、0023、0025、0026，其余23题内容保持审核表结果。',
           'submitted_review.csv 是收到的审核表原样副本；revisions.jsonl 记录逐题处理。',
           '表格解析提示、标题作为身份锚点等事项请在 source_quality 中确认；没有名为 heading_only 的人工确认字段。',
           '原始知识库、解析快照、KG及trial_v2（包括已填写意见的CSV）均未修改。','']
    (args.output/'README.md').write_text('\n'.join(lines),encoding='utf-8')
    print(json.dumps({'output':str(args.output),'candidates':len(rows),'priority_re_review':len(priority)},ensure_ascii=False))


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
