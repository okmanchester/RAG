"""Immutable 150-question gold release and deterministic grouped 120/30 split."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from .core import (Corpus, digest, duplicate_clusters, evidence_families, export_review,
                   file_hash, load_release, now, quotas, read_jsonl, schema_version,
                   validate_item, write_json, write_jsonl)
from .quality import content_fingerprint, span

SPLIT_SEED = 20260929
SPLIT_FIELDS = ('difficulty', 'evidence_span', 'expected_behavior', 'query_style', 'answer_form')


def adopt_reviews(items, candidates, corpus, authorization):
    """Accept attributed prior agent approvals, without rewriting historical inputs."""
    if not isinstance(authorization, str) or not authorization.strip():
        raise ValueError('Gold150 freeze requires recorded user authorization')
    originals = {i['id']: i for i in candidates}
    if len(originals) != len(candidates):
        raise ValueError('Duplicate source candidate IDs')
    approved, ledger = [], []
    for source in items:
        row = copy.deepcopy(source)
        if row.get('review_status') == 'pending':
            audit = row.get('agent_review', {})
            original = originals.get(row['id'])
            prior = {k: v for k, v in source.items() if k != 'agent_review'}
            if (original is None or audit.get('id') != row['id']
                    or audit.get('source_candidate_hash') != digest(original)
                    or digest(prior) != digest(original)):
                raise ValueError(f'Approval source fingerprint mismatch: {row["id"]}')
            if (audit.get('agent_review_status') != 'approved' or audit.get('failed_checks') != []
                    or not audit.get('reviewer', '').strip() or not audit.get('reviewed_at')):
                raise ValueError(f'Missing attributed prior approval: {row["id"]}')
            row.update(review_status='approved', reviewer=audit['reviewer'],
                       review_checks=copy.deepcopy(audit['agent_review_checks']),
                       reviewed_at=audit['reviewed_at'],
                       review_notes=(row.get('review_notes', '') + '\n[冻结时接纳已有审核]\n'
                                     + audit.get('finding', '')).strip())
            row['approval_provenance'] = {
                'method': 'user_authorized_adoption_of_prior_agent_review',
                'authorization': authorization, 'reviewer': audit['reviewer'],
                'at': now(), 'prior_reviewed_at': audit['reviewed_at'],
                'source_candidate_sha256': digest(original),
                'human_per_item_review_claimed': False, 'independent_reviewer': False,
                'audit_file': 'review_provenance/approval_adoptions.jsonl',
            }
            ledger.append({'id': row['id'], 'source_pool_item_sha256': digest(source),
                           'source_candidate_sha256': digest(original),
                           'original_review_status': source['review_status'],
                           'accepted_review': copy.deepcopy(audit),
                           'approval_provenance': row['approval_provenance']})
        validate_item(row, corpus, approved=True)
        if (row['id'] != source['id'] or row['category'] != source['category']
                or content_fingerprint(row) != content_fingerprint(source)):
            raise ValueError('Approval adoption changed question content')
        approved.append(row)
    return approved, ledger


def shared_evidence_groups(items):
    """Connected components of shared document/block pairs across all alternatives."""
    parent = list(range(len(items)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    by_block = defaultdict(list)
    for n, row in enumerate(items):
        keys = {(e['doc_id'], e['block_id']) for g in row['evidence_groups']
                for bundle in g['alternatives'] for e in bundle}
        for key in sorted(keys):
            by_block[key].append(n)
    for indices in by_block.values():
        for i in indices[1:]:
            parent[root(i)] = root(indices[0])
    groups = defaultdict(list)
    for i in range(len(items)):
        groups[root(i)].append(i)
    return list(groups.values())


def select_gold150(items):
    """Exact grouped split; lexicographic SHA-256 tie break among optimal strata."""
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import lil_matrix, vstack

    if (len(items) != 150 or schema_version(items) != 2
            or len({i['id'] for i in items}) != 150
            or Counter(i['category'] for i in items) != {'policy': 75, 'technical': 75}):
        raise ValueError('Gold150 requires exactly 150 distinct schema-v2 items, 75 per category')
    rows = sorted(copy.deepcopy(items), key=lambda i: i['id'])
    n = len(rows)
    labels = []
    for field in SPLIT_FIELDS:
        def values(row):
            return row[field] if field == 'answer_form' else [row.get(field)]
        for value in sorted({v for r in rows for v in values(r)}, key=lambda v: (v is not None, str(v))):
            labels.append((field, value, [i for i, row in enumerate(rows) if value in values(row)]))
    size = n + len(labels)
    constraints = []

    def add(coefs, low, high):
        constraints.append((coefs, low, high))

    add({i: 1 for i in range(n)}, 30, 30)
    for category in ('policy', 'technical'):
        add({i: 1 for i, row in enumerate(rows) if row['category'] == category}, 15, 15)
    groups = shared_evidence_groups(rows)
    for group in groups:
        for i in group[1:]:
            add({group[0]: 1, i: -1}, 0, 0)
    for i, row in enumerate(rows):
        if row.get('difficulty') == 'hard' or row['expected_behavior'] == 'clarify':
            add({i: 1}, 0, 0)
    # Integer-scaled |5 * holdout_count - pool_count| avoids fractional objectives.
    for k, (_, _, indices) in enumerate(labels):
        add({**{i: 5 for i in indices}, n+k: -1}, -np.inf, len(indices))
        add({**{i: -5 for i in indices}, n+k: -1}, -np.inf, -len(indices))
    matrix = lil_matrix((len(constraints), size))
    for k, (coefs, _, _) in enumerate(constraints):
        for j, value in coefs.items():
            matrix[k, j] = value
    matrix = matrix.tocsr()
    low = np.array([c[1] for c in constraints])
    high = np.array([c[2] for c in constraints])
    lower = np.zeros(size)
    upper = np.r_[np.ones(n), np.full(len(labels), np.inf)]
    objective = np.r_[np.zeros(n), np.ones(len(labels))]

    def solve(cost):
        return milp(cost, integrality=np.ones(size), bounds=Bounds(lower, upper),
                    constraints=LinearConstraint(matrix, low, high),
                    options={'time_limit': 30, 'mip_rel_gap': 0})

    result = solve(objective)
    if not result.success:
        raise ValueError(f'Cannot satisfy Gold150 split constraints: {result.message}')
    optimum = int(round(result.fun))
    matrix = vstack([matrix, objective.reshape(1, -1)], format='csr')
    low = np.r_[low, optimum]
    high = np.r_[high, optimum]
    ordered = sorted(range(n), key=lambda i: (
        hashlib.sha256(f'{SPLIT_SEED}:{rows[i]["id"]}'.encode('utf-8')).hexdigest(), rows[i]['id']))
    witness = result.x
    for i in ordered:
        # A current feasible witness avoids a solver call when this bit is already one.
        lower[i] = upper[i] = 1
        if witness[i] > .5:
            continue
        attempt = solve(np.zeros(size))
        if attempt.success:
            witness = attempt.x
        elif attempt.status == 2:  # Proven infeasible, not a time limit or solver error.
            lower[i] = upper[i] = 0
        else:
            raise ValueError(f'Gold150 deterministic tie break failed: {attempt.message}')
    selected = [dict(row, split='holdout' if lower[i] else 'dev') for i, row in enumerate(rows)]
    for split, count, category_count in (('dev', 120, 60), ('holdout', 30, 15)):
        part = [r for r in selected if r['split'] == split]
        if len(part) != count or Counter(r['category'] for r in part) != {
                'policy': category_count, 'technical': category_count}:
            raise ValueError('Gold150 solver returned an invalid partition')
    if any(len({selected[i]['split'] for i in group}) != 1 for group in groups):
        raise ValueError('Shared evidence crosses splits')
    deviation = sum(abs(5*sum(selected[i]['split'] == 'holdout' for i in indices)-len(indices))
                    for _, _, indices in labels)
    if deviation != optimum:
        raise ValueError('Gold150 solver returned a nonoptimal partition')
    selection = {
        'method': 'all_150_items_grouped_stratified_milp', 'seed': SPLIT_SEED,
        'objective': 'sum_abs_5_times_holdout_count_minus_pool_count',
        'objective_fields': list(SPLIT_FIELDS), 'optimal_scaled_deviation': optimum,
        'tie_break': 'sha256_utf8_seed_colon_id_ascending_prefer_holdout_if_optimal_feasible',
        'shared_evidence_groups': [[rows[i]['id'] for i in group] for group in groups if len(group) > 1],
        'dev_only_ids': [r['id'] for r in rows if r.get('difficulty') == 'hard'
                         or r['expected_behavior'] == 'clarify'],
        'assignment': {r['id']: r['split'] for r in selected},
    }
    return selected, selection


def _record_ids(value):
    if isinstance(value, dict):
        found = {value['id']} if isinstance(value.get('id'), str) else set()
        if isinstance(value.get('results'), dict):
            found.update(value['results'])
        return found | set().union(*(_record_ids(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_record_ids(v) for v in value))
    return set()


def freeze_gold150(items, corpus, families, output, manifest, authorization,
                   source_candidates=(), reference_releases=(), review_artifacts=(),
                   source_items_path=None):
    """Freeze the entire reviewed pool, explicitly allowing historical public items."""
    output = Path(output)
    if output.exists():
        raise FileExistsError(f'Immutable release already exists: {output}')
    if manifest['corpus'] != corpus.identity():
        raise ValueError('Corpus changed since review')
    mapping = families['family_by_doc']
    if (set(mapping) != set(corpus.documents)
            or any(v not in mapping or mapping[v] != v for v in mapping.values())
            or families.get('review_status') != 'approved' or not families.get('reviewer', '').strip()):
        raise ValueError('Invalid or unreviewed document family mapping')
    exclusions = families.get('coverage_exclusions', {})
    if (not set(exclusions) <= set(mapping.values())
            or any(not str(reason).strip() for reason in exclusions.values())):
        raise ValueError('Invalid document family coverage exclusions')
    approved, adoptions = adopt_reviews(items, source_candidates, corpus, authorization)
    for row in approved:
        if span(row) == 'cross_document' and len(evidence_families(row, mapping)) < 2:
            raise ValueError('Cross-document question uses duplicate document versions')
    _, links = duplicate_clusters(approved)
    if links:
        raise ValueError(f'Duplicate Gold150 questions: {links}')
    covered = set().union(*(evidence_families(row, mapping) for row in approved))
    coverage = {'covered_family_ids': sorted(covered),
                'uncovered_family_ids': sorted(set(mapping.values()) - set(exclusions) - covered),
                'coverage_exclusions': copy.deepcopy(exclusions),
                'all_families_coverage_required': False}
    if sum(r['query_style'] == 'document' for r in approved) > 15:
        raise ValueError('Gold150 document-hint cap is 10 percent')

    references, public_sources, source_locations = [], defaultdict(list), {}
    by_id = {i['id']: i for i in items}
    for directory in reference_releases:
        directory = Path(directory)
        previous, previous_manifest = load_release(directory)
        if (previous_manifest['corpus'] != manifest['corpus']
                or previous_manifest.get('judge') != manifest.get('judge')):
            raise ValueError('Reference release corpus/judge differs from Gold150')
        for row in previous:
            if row['id'] not in by_id or content_fingerprint(row) != content_fingerprint(by_id[row['id']]):
                raise ValueError('Reference release item missing or changed in Gold150')
            public_sources[row['id']].append(directory.as_posix())
            source_locations[row['id']] = directory.resolve()
        references.append({'release': directory.as_posix(), 'item_count': len(previous),
                           'gold_sha256': previous_manifest['gold_sha256'],
                           'manifest_sha256': file_hash(directory/'manifest.json')})
    if source_items_path is not None and read_jsonl(source_items_path) != items:
        raise ValueError('Source pool file differs from supplied items')
    artifacts = sorted({Path(p).resolve() for p in review_artifacts}, key=lambda p: p.as_posix())
    if any(not p.is_file() for p in artifacts):
        raise ValueError('Review artifacts must be existing files')
    selected, selection = select_gold150(approved)
    original_by_id = {r['id']: r for r in items}
    for row in selected:
        original = original_by_id[row['id']]
        row['freeze_provenance'] = {
            'method': 'user_authorized_gold150_repartition', 'authorization': authorization,
            'source_pool_item_sha256': digest(original),
            'original_review_status': original.get('review_status'),
            'original_split': original.get('split'), 'original_pilot': original.get('pilot'),
            'public_source_releases': public_sources[row['id']],
            'previously_public': bool(public_sources[row['id']] or original.get('pilot')),
            'new64_previously_used_for_tuning': False,
            'holdout_use': 'reserved_from_this_freeze_not_claimed_unseen',
            'human_per_item_review_claimed': False,
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.freeze-gold150-', dir=output.parent))
    try:
        artifact_index = []
        for source in artifacts:
            try:
                name = source.relative_to(Path.cwd().resolve()).as_posix()
            except ValueError:
                name = hashlib.sha256(str(source.parent).encode()).hexdigest()[:16] + '/' + source.name
            relative = 'review_provenance/sources/' + name
            target = temporary/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            ids = set()
            if source.suffix == '.jsonl':
                ids = _record_ids(read_jsonl(source))
            elif source.suffix == '.json':
                ids = _record_ids(json.loads(source.read_text(encoding='utf-8-sig')))
            artifact_index.append({'source_path': name, 'artifact_path': relative,
                                   'sha256': file_hash(target), 'question_ids': sorted(ids),
                                   '_source': source})
        # Current audit pointers resolve inside the new release; untouched historical objects
        # remain available byte-for-byte in source_pool150 and the namespaced archives.
        for row in selected:
            for field, key in (('review_provenance', 'audit_file'), ('approval_provenance', 'audit_file'),
                               ('corpus_search_provenance', 'current_search_file')):
                provenance = row.get(field, {})
                original_path = provenance.get(key)
                if not original_path or original_path.startswith('review_provenance/approval_adoptions'):
                    continue
                matches = [a for a in artifact_index if a['_source'].name == Path(original_path).name
                           and row['id'] in a['question_ids']]
                origin = source_locations.get(row['id'])
                exact = [a for a in artifact_index if origin is not None
                         and a['_source'] == (origin/original_path).resolve()]
                match = exact or matches
                if len(match) == 1:
                    provenance[key] = match[0]['artifact_path']
                elif artifact_index:
                    raise ValueError(f'Unresolved or ambiguous review artifact: {row["id"]}/{original_path}')
        for record in artifact_index:
            record.pop('_source')
        write_json(temporary/'review_provenance/artifact_index.json', artifact_index)
        if source_items_path is not None:
            shutil.copyfile(source_items_path, temporary/'review_provenance/source_pool150.jsonl')
        else:
            write_jsonl(temporary/'review_provenance/source_pool150.jsonl', items)
        write_jsonl(temporary/'review_provenance/source_candidates.jsonl', source_candidates)
        write_jsonl(temporary/'review_provenance/approval_adoptions.jsonl', adoptions)
        write_json(temporary/'review_provenance/source_manifest.json', manifest)
        write_json(temporary/'split.json', selection)
        write_jsonl(temporary/'gold.jsonl', selected)
        write_json(temporary/'families.json', families)
        for name in ('documents.jsonl', 'chunks.jsonl'):
            shutil.copyfile(corpus.directory/name, temporary/name)
        export_review(selected, temporary/'review', corpus)
        for split in ('dev', 'holdout'):
            part = [r for r in selected if r['split'] == split]
            write_jsonl(temporary/split/'gold.jsonl', part)
            write_jsonl(temporary/split/'queries.jsonl', [{'id':r['id'], 'user_input':r['user_input']} for r in part])
            write_jsonl(temporary/split/'run_template.jsonl', [
                {'id':r['id'], 'response':'', 'retrieved_contexts':[], 'citations':[],
                 'run_config':{'pipeline_version':'REPLACE_WITH_ACTUAL_RUN_CONFIG'}, 'error':'not_run'} for r in part])
        exposed = [r['id'] for r in selected if r['split'] == 'holdout' and r['freeze_provenance']['previously_public']]
        (temporary/'README.md').write_text(
            '# 150题金标准 v1\n\n状态：已冻结。完整gold.jsonl包含150题，政策/技术各75题。\n'
            'dev/为120题调优集（政策/技术各60题）；holdout/为30题留出集（各15题）。\n'
            '只将对应queries.jsonl交给被测系统；运行模板默认not_run，不表示系统已运行。\n'
            '子目录是同一发布的划分文件，不是独立release；评测--release始终指向本目录。\n'
            'Git只需保留11个核心文件，审核历史及分集gold副本在本地保存。\n'
            '默认加载校验核心；verify-release --include-history校验全部本地历史。\n\n'
            f'冻结授权：{authorization}\n\n'
            '138题沿用正式审核，另外12题由用户授权接纳已有逐题agent审核；未声称独立人工复审。\n'
            '原始输入、审批接纳及来源资料在review_provenance/，归档索引记录原路径及SHA-256。\n'
            'review/是新版本审核归档；历史原始对象保持原样。\n\n'
            f'固定种子{SPLIT_SEED}，全池重新分层，共享证据块不跨集合，困难/澄清单例留在dev。\n'
            f'本版留出集有{len(exposed)}道旧公开题，详情见manifest及逐题freeze_provenance；'
            '这是从本次冻结起停止参与调参的留出集，不声称从未见过的盲测集。新增64题此前未用于调参。\n'
            '日常调优只使用dev；关键版本固定配置后才评holdout，不根据其逐题失败迭代参数。\n\n'
            '在conda rag环境中，从仓库根目录运行：\n\n```powershell\n'
            f'python -m rag_baseline.benchmark evaluate --release {output.as_posix()} --runs tmp/dev_runs.jsonl --output tmp/dev_eval\n'
            f'python -m rag_baseline.benchmark evaluate --release {output.as_posix()} --runs tmp/holdout_runs.jsonl --output tmp/holdout_eval --split holdout\n'
            '```\n\n所有发布文件由manifest校验哈希；任何修订必须新发版本，禁止覆盖本目录。\n', encoding='utf-8')
        hashes = {p.relative_to(temporary).as_posix():file_hash(p)
                  for p in sorted(temporary.rglob('*')) if p.is_file()}
        frozen_manifest = {k:copy.deepcopy(manifest[k]) for k in ('corpus', 'judge', 'generator', 'evidence_policy') if k in manifest}
        frozen_manifest.update({
            'status':'frozen', 'dataset_kind':'gold150', 'schema_version':2,
            'release':output.name, 'frozen_at':now(), 'item_count':150,
            'freeze_authorization':authorization, 'split_policy':'full_pool_repartition_120_30',
            'selection':selection, 'source_items_sha256':digest(items),
            'source_pool_file_sha256':hashes['review_provenance/source_pool150.jsonl'],
            'previous_frozen_releases':references, 'approval_adoption_count':len(adoptions),
            'public_item_holdout_override':True, 'new64_previously_used_for_tuning':False,
            'holdout_previously_public_ids':exposed, 'holdout_previously_public_count':len(exposed),
            'holdout_is_claimed_unseen':False, 'full_benchmark_quotas_applied':False,
            'no_freeze_performed':False, 'quotas':{s:quotas(selected, s) for s in ('dev','holdout')},
            'gold_sha256':hashes['gold.jsonl'], 'families_sha256':hashes['families.json'],
            'duplicate_links':links, 'artifacts_sha256':hashes,
            'document_family_coverage':coverage,
            'implementation_sha256':{name:file_hash(Path(__file__).with_name(name))
                                     for name in ('gold150.py','core.py','quality.py')},
            'protected_input_sha256':manifest.get('protected_input_sha256', {}),
        })
        write_json(temporary/'manifest.json', frozen_manifest)
        load_release(temporary, verify_history=True)
        for name, expected in manifest.get('protected_input_sha256', {}).items():
            if file_hash(name) != expected:
                raise ValueError(f'Protected freeze input was modified: {name}')
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(temporary)
        raise
