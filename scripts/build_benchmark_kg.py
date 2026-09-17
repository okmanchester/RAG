"""为现有可检索 chunks 构建 Ragas KG，支持阶段恢复和共享 LLM 缓存。"""

import argparse
import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import ragas
from langchain_core.documents import Document
from openai import AsyncOpenAI
from ragas.cache import DiskCacheBackend
from ragas.embeddings import HuggingFaceEmbeddings
from ragas.llms import llm_factory
from ragas.run_config import RunConfig
from ragas.testset.graph import KnowledgeGraph, Node, NodeType
from ragas.testset.transforms import (
    CosineSimilarityBuilder,
    EmbeddingExtractor,
    OverlapScoreBuilder,
    SummaryExtractor,
    apply_transforms,
)
from ragas.testset.transforms.base import BaseGraphTransformation
from ragas.testset.transforms.extractors import NERExtractor
from ragas.testset.transforms.extractors.llm_based import ThemesExtractor

CHUNKS_PATH = Path("data/processed/baseline_v1/chunks.jsonl")
BENCHMARK_DIR = Path("data/benchmark")
FINAL_KG_PATH = BENCHMARK_DIR / "knowledge_graph.json"
CACHE_DIR = BENCHMARK_DIR / "ragas_cache"
EMBEDDING_MODEL_PATH = r"D:\WorkSpace\AIModels\BAAI\bge-large-zh-v1.5"
LLM_MODEL = "deepseek-flash"
LLM_BASE_URL = "https://api.deepseek.com"
SMOKE_SIZE = 20
LLM_MAX_TOKENS = 4096
OPENAI_MAX_RETRIES = 2  # SDK 网络层重试次数
STRUCTURED_OUTPUT_MAX_ATTEMPTS = 2  # Instructor max_retries 表示总尝试次数
MAX_WORKERS = 4
REQUEST_TIMEOUT_SECONDS = 180
SUMMARY_SIMILARITY_THRESHOLD = 0.7
NER_OVERLAP_THRESHOLD = 0.01
PIPELINE_VERSION = 2
STAGE_NAMES = (
    "summary", "summary_embedding", "themes", "ner",
    "summary_similarity", "ner_overlap",
)


@dataclass(frozen=True)
class Stage:
    name: str
    transform: BaseGraphTransformation
    property_name: str | None = None
    relationship_type: str | None = None


def load_chunks(path: Path) -> list[Document]:
    documents = []
    with path.open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            chunk = json.loads(line)
            if not chunk.get("retrieval_enabled", True):
                continue
            text = chunk.get("text", "").strip()
            if not text:
                raise ValueError(f"第 {line_no} 行存在空的 retrieval-enabled chunk")
            metadata = {
                key: chunk[key]
                for key in ("chunk_id", "chunk_index", "doc_id", "source_path")
            }
            metadata.update({
                key: chunk.get(key)
                for key in (
                    "category", "title", "section_id", "section_char_start",
                    "section_char_end", "content_type",
                )
            })
            metadata["heading_path"] = chunk.get("heading_path", [])
            metadata["pages"] = sorted({
                source["page"] for source in chunk.get("sources", [])
                if source.get("page") is not None
            })
            documents.append(Document(page_content=text, metadata=metadata))
    if not documents:
        raise ValueError("没有读取到任何可检索 chunk")
    return documents


def select_smoke_chunks(chunks: list[Document], size: int) -> list[Document]:
    """一半选最长文本，一半在剩余语料中均匀取点。"""
    if len(chunks) <= size:
        return chunks
    longest = sorted(chunks, key=lambda doc: len(doc.page_content), reverse=True)[:size // 2]
    selected_ids = {doc.metadata["chunk_id"] for doc in longest}
    remaining = [doc for doc in chunks if doc.metadata["chunk_id"] not in selected_ids]
    count = size - len(longest)
    indexes = (
        [len(remaining) // 2] if count == 1 else
        [round(i * (len(remaining) - 1) / (count - 1)) for i in range(count)]
    )
    return longest + [remaining[index] for index in indexes]


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build_identity(mode: str, chunks: list[Document]) -> dict:
    selected_ids = "".join(doc.metadata["chunk_id"] + "\n" for doc in chunks)
    return {
        "pipeline_version": PIPELINE_VERSION,
        "stages": list(STAGE_NAMES),
        "mode": mode,
        "chunks_file_sha256": sha256_file(CHUNKS_PATH),
        "selected_chunks_sha256": hashlib.sha256(selected_ids.encode("utf-8")).hexdigest(),
        "selected_chunk_count": len(chunks),
        "ragas_version": ragas.__version__,
        "llm_model": LLM_MODEL,
        "llm_max_tokens": LLM_MAX_TOKENS,
        "thinking": "disabled",
        "embedding_model_path": str(Path(EMBEDDING_MODEL_PATH).resolve()),
        "summary_similarity_threshold": SUMMARY_SIMILARITY_THRESHOLD,
        "ner_overlap_threshold": NER_OVERLAP_THRESHOLD,
    }


def build_llm():
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("缺少环境变量 DEEPSEEK_API_KEY，请设置后再运行构建。")
    client = AsyncOpenAI(
        api_key=api_key, base_url=LLM_BASE_URL,
        timeout=REQUEST_TIMEOUT_SECONDS, max_retries=OPENAI_MAX_RETRIES,
    )
    return llm_factory(
        LLM_MODEL, client=client,
        cache=DiskCacheBackend(cache_dir=str(CACHE_DIR)),
        max_tokens=LLM_MAX_TOKENS,
        max_retries=STRUCTURED_OUTPUT_MAX_ATTEMPTS,
        extra_body={"thinking": {"type": "disabled"}},
    )


def build_embeddings():
    return HuggingFaceEmbeddings(model=EMBEDDING_MODEL_PATH, device="cuda")


def build_initial_kg(chunks: list[Document]) -> KnowledgeGraph:
    return KnowledgeGraph(nodes=[
        Node(type=NodeType.CHUNK, properties={
            "page_content": doc.page_content,
            "document_metadata": doc.metadata,
        })
        for doc in chunks
    ])


def is_chunk(node: Node) -> bool:
    return node.type == NodeType.CHUNK


def missing_property(property_name: str):
    # Ragas 先 extract 再检查属性，提前过滤也能在缓存丢失时避免重复调用。
    def needs_extraction(node: Node) -> bool:
        return is_chunk(node) and node.get_property(property_name) is None
    return needs_extraction


def build_stages(llm, embeddings) -> list[Stage]:
    return [
        Stage("summary", SummaryExtractor(
            llm=llm, filter_nodes=missing_property("summary"),
        ), property_name="summary"),
        Stage("summary_embedding", EmbeddingExtractor(
            embedding_model=embeddings, property_name="summary_embedding",
            embed_property_name="summary", filter_nodes=missing_property("summary_embedding"),
        ), property_name="summary_embedding"),
        Stage("themes", ThemesExtractor(
            llm=llm, filter_nodes=missing_property("themes"),
        ), property_name="themes"),
        Stage("ner", NERExtractor(
            llm=llm, filter_nodes=missing_property("entities"),
        ), property_name="entities"),
        Stage("summary_similarity", CosineSimilarityBuilder(
            property_name="summary_embedding", new_property_name="summary_similarity",
            threshold=SUMMARY_SIMILARITY_THRESHOLD, filter_nodes=is_chunk,
        ), relationship_type="summary_similarity"),
        Stage("ner_overlap", OverlapScoreBuilder(
            threshold=NER_OVERLAP_THRESHOLD, filter_nodes=is_chunk,
        ), relationship_type="entities_overlap"),
    ]


def validate_extractor(kg: KnowledgeGraph, stage: Stage, report_dir: Path, *, strict=True):
    nodes = [node for node in kg.nodes if is_chunk(node)]
    missing = [node for node in nodes if node.get_property(stage.property_name) is None]
    print(f"[coverage] {stage.property_name}: {len(nodes) - len(missing)}/{len(nodes)}")
    missing_path = report_dir / f"missing_{stage.name}.jsonl"
    if not missing:
        missing_path.unlink(missing_ok=True)
        return
    report_dir.mkdir(parents=True, exist_ok=True)
    with missing_path.open("w", encoding="utf-8") as stream:
        for node in missing:
            metadata = node.get_property("document_metadata") or {}
            record = {"node_id": str(node.id)}
            record.update({key: metadata.get(key) for key in (
                "chunk_id", "doc_id", "source_path", "title",
            )})
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    if strict:
        raise RuntimeError(
            f"Stage '{stage.name}' 仍有 {len(missing)}/{len(nodes)} nodes "
            f"缺少 '{stage.property_name}'。缺失列表：{missing_path.resolve()}"
        )


def clear_relationship_type(kg: KnowledgeGraph, relationship_type: str):
    # KG 和 state 分别原子保存，若二者之间中断，旧 stage 会重跑。
    kg.relationships = [rel for rel in kg.relationships if rel.type != relationship_type]


def atomic_save_kg(kg: KnowledgeGraph, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    kg.save(temp_path)
    os.replace(temp_path, path)


def atomic_save_json(value: dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


@dataclass(frozen=True)
class Checkpoint:
    directory: Path
    identity: dict

    @property
    def kg_path(self) -> Path:
        return self.directory / "knowledge_graph.checkpoint.json"

    @property
    def state_path(self) -> Path:
        return self.directory / "state.json"

    @property
    def report_dir(self) -> Path:
        return self.directory / "reports"

    def save(self, kg: KnowledgeGraph, next_stage_index: int, failed_stage=None):
        atomic_save_kg(kg, self.kg_path)
        atomic_save_json({
            "build_identity": self.identity,
            "next_stage_index": next_stage_index,
            "failed_stage": failed_stage,
            "nodes": len(kg.nodes),
            "relationships": len(kg.relationships),
        }, self.state_path)

    def load(self):
        if not self.kg_path.exists() and not self.state_path.exists():
            return None
        if self.kg_path.exists() != self.state_path.exists():
            raise RuntimeError(f"Checkpoint 不完整：{self.directory}，请检查或使用 --reset。")
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        if state.get("build_identity") != self.identity:
            raise RuntimeError("现有 checkpoint 与当前语料或构建配置不兼容，请使用 --reset。")
        index = state.get("next_stage_index")
        if type(index) is not int or not 0 <= index <= len(STAGE_NAMES):
            raise RuntimeError("Checkpoint 阶段索引无效，请使用 --reset。")
        return KnowledgeGraph.load(self.kg_path), index


def run_stage(kg: KnowledgeGraph, stage: Stage, index: int, checkpoint: Checkpoint):
    print(f"\nStage {index + 1}/{len(STAGE_NAMES)}: {stage.name}")
    if stage.relationship_type:
        clear_relationship_type(kg, stage.relationship_type)
    try:
        apply_transforms(kg, stage.transform, run_config=RunConfig(max_workers=MAX_WORKERS))
        if stage.property_name:
            validate_extractor(kg, stage, checkpoint.report_dir)
    except BaseException:
        checkpoint.save(kg, index, failed_stage=stage.name)
        # 记录缺失清单，同时保留原始抽取异常或 KeyboardInterrupt。
        if stage.property_name:
            validate_extractor(kg, stage, checkpoint.report_dir, strict=False)
        print(f"[FAILED] 部分 KG 已保存：{checkpoint.kg_path.resolve()}；重跑同一命令可继续。")
        raise
    checkpoint.save(kg, index + 1)
    print(f"[OK] {len(kg.nodes)} nodes, {len(kg.relationships)} relationships")


def reset_mode(mode: str):
    root = (BENCHMARK_DIR / "kg_build").resolve()
    directory = (root / mode).resolve()
    if mode not in ("smoke", "full") or directory.parent != root:
        raise ValueError("无效的 reset 目录")
    if directory.exists():
        shutil.rmtree(directory)
        print(f"Removed checkpoint directory: {directory}")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode", choices=["smoke", "full"], default="smoke",
        help="smoke: 少量 chunk 验证整条 pipeline；full: 全量可检索 chunks",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="删除当前 mode 的 checkpoint 后重新开始，保留共享 LLM cache。",
    )
    return parser.parse_args()


def export_kg(kg: KnowledgeGraph, mode: str):
    path = BENCHMARK_DIR / "knowledge_graph.smoke.json" if mode == "smoke" else FINAL_KG_PATH
    atomic_save_kg(kg, path)
    print(f"\nKG BUILD COMPLETE: {len(kg.nodes)} nodes, {len(kg.relationships)} relationships")
    for kind, count in sorted(Counter(rel.type for rel in kg.relationships).items()):
        print(f"  {kind}: {count}")
    print(f"Saved: {path.resolve()} ({path.stat().st_size / 1024 / 1024:.2f} MiB)")
    if mode == "smoke":
        print("下一步：python scripts/build_benchmark_kg.py --mode full")


def main():
    args = parse_args()
    all_chunks = load_chunks(CHUNKS_PATH)
    chunks = select_smoke_chunks(all_chunks, SMOKE_SIZE) if args.mode == "smoke" else all_chunks
    print(f"Corpus chunks: {len(all_chunks)}; mode: {args.mode}; selected: {len(chunks)}")
    checkpoint = Checkpoint(BENCHMARK_DIR / "kg_build" / args.mode, build_identity(args.mode, chunks))
    if args.reset:
        reset_mode(args.mode)
    saved = checkpoint.load()
    kg, next_index = saved if saved is not None else (build_initial_kg(chunks), 0)
    if next_index == len(STAGE_NAMES):
        export_kg(kg, args.mode)
        return

    # 校验恢复配置后才加载模型；完整 checkpoint 的重新导出不需要密钥或 GPU。
    llm = build_llm()
    embeddings = build_embeddings()
    stages = build_stages(llm, embeddings)
    if saved is None:
        checkpoint.save(kg, 0)
    print(f"Pipeline: {' -> '.join(STAGE_NAMES)}; resume index: {next_index}")
    for index in range(next_index, len(stages)):
        run_stage(kg, stages[index], index, checkpoint)
    export_kg(kg, args.mode)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        raise SystemExit(130)
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
