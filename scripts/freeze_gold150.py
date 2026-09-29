"""Freeze the latest reviewed 150-question pool, locally and without model calls."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rag_baseline.benchmark.core import Corpus, file_hash, load_release, read_jsonl
from rag_baseline.benchmark.gold150 import freeze_gold150


def main():
    parser = argparse.ArgumentParser(description='冻结完整150题并确定性划分120/30')
    parser.add_argument('--output', type=Path, default=Path('data/benchmark/gold150_final_v1'))
    parser.add_argument('--authorization', required=True, help='本次用户明确授权的冻结指令')
    args = parser.parse_args()
    if Path.cwd().resolve() != ROOT:
        raise ValueError('Run this script from the repository root')
    if args.output.exists():
        raise FileExistsError(f'Immutable release already exists: {args.output}')
    base = Path('data/benchmark')
    source = base/'batch_002_authored10/review_v2'
    references = [base/'trial30_final_v1', base/'batch56_final_v1']
    _, old_manifest = load_release(references[1])
    families = json.loads((references[1]/'families.json').read_text(encoding='utf-8'))
    latest_families = json.loads((source/'families.json').read_text(encoding='utf-8'))
    if (families['family_by_doc'] != latest_families['family_by_doc']
            or families.get('coverage_exclusions', {}) != latest_families.get('coverage_exclusions', {})):
        raise ValueError('Latest family mapping differs from the reviewed frozen mapping')
    corpus = Corpus('data/processed/baseline_v1')
    for document in corpus.documents.values():
        if file_hash(Path('data/raw')/document['source_path']) != document['sha256']:
            raise ValueError(f'Raw source differs from frozen corpus: {document["source_path"]}')
    history = [base/'trial_v3', base/'batch_002_review64/agent_review_v1',
               base/'batch_002_review64/revision_v2', base/'batch_002_authored10']
    artifacts = sorted({p for directory in references+history for p in directory.rglob('*')
                        if p.is_file() and p.suffix in {'.json', '.jsonl', '.csv', '.md'}
                        and p.name not in {'documents.jsonl', 'chunks.jsonl'}}, key=lambda p:p.as_posix())
    candidate_path = base/'batch_002_review64/candidates.jsonl'
    artifacts.append(candidate_path)
    protected_paths = set(artifacts)
    protected_paths.update(p for directory in references for p in directory.rglob('*') if p.is_file())
    protected_paths.update(Path('data/raw')/d['source_path'] for d in corpus.documents.values())
    protected_paths.update(corpus.directory/name for name in ('documents.jsonl','chunks.jsonl'))
    protected = {p.as_posix():file_hash(p) for p in sorted(protected_paths)}
    manifest = {k:old_manifest[k] for k in ('corpus','judge','generator','evidence_policy') if k in old_manifest}
    manifest.update(status='approved_gold150_pool', source_batch=source.as_posix(),
                    source_items=(source/'pool150.jsonl').as_posix(),
                    families_source=(references[1]/'families.json').as_posix(),
                    protected_input_sha256=protected,
                    implementation_script_sha256=file_hash(__file__),
                    user_selected_full_pool_repartition=True,
                    user_selected_proportional_stratification=True,
                    user_confirmed_new64_not_used_for_tuning=True)
    freeze_gold150(read_jsonl(source/'pool150.jsonl'), corpus, families, args.output,
                   manifest, args.authorization, source_candidates=read_jsonl(candidate_path),
                   reference_releases=references, review_artifacts=artifacts,
                   source_items_path=source/'pool150.jsonl')
    gold, frozen = load_release(args.output, verify_history=True)
    print(json.dumps({'release':args.output.as_posix(), 'status':frozen['status'],
                      'item_count':len(gold), 'split_counts':{s:frozen['quotas'][s]['total'] for s in ('dev','holdout')},
                      'approval_adoption_count':frozen['approval_adoption_count'],
                      'previously_public_holdout_count':frozen['holdout_previously_public_count'],
                      'protected_inputs_verified':len(protected)}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
