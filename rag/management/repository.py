"""Data access helpers. Transaction boundaries belong to the service layer."""
from sqlalchemy import func, select

from .errors import ManagementError

from .models import Chunk, Document, DocumentBuild, DocumentVersion, ImportRun, KnowledgeBase


def required(session, model, identifier, *, lock=False):
    statement = select(model).where(model.id == identifier)
    if lock:
        statement = statement.with_for_update()
    item = session.scalar(statement)
    if item is None:
        raise ManagementError("RECORD_NOT_FOUND")
    return item


def record(item):
    return {column.name: getattr(item, column.name) for column in item.__table__.columns}


def build_document(session, build):
    version = required(session, DocumentVersion, build.document_version_id)
    return version.document_id


def chunk_count(session, build_id, *, eligible=False):
    statement = select(func.count()).select_from(Chunk).where(Chunk.build_id == build_id)
    if eligible:
        statement = statement.where(Chunk.retrieval_enabled.is_(True))
    return session.scalar(statement)


def current_build(session, document_id):
    document = required(session, Document, document_id)
    kb = required(session, KnowledgeBase, document.knowledge_base_id)
    if document.status != "active" or kb.status != "active":
        raise ManagementError("RESOURCE_DISABLED")
    if document.active_build_id is None:
        raise ManagementError("NO_ACTIVE_BUILD")
    return document.active_build_id


MODELS = {"kb": KnowledgeBase, "document": Document, "version": DocumentVersion,
          "build": DocumentBuild, "chunk": Chunk, "import": ImportRun}
