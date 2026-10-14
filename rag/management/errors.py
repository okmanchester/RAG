"""Only catalogued messages cross the CLI/database error boundary."""
import json
import re

from sqlalchemy.exc import SQLAlchemyError


MESSAGES = {
    "SCHEMA_OUTDATED": "database schema is not current; run python -m rag.manage db upgrade",
    "INVALID_KB_CODE": "kb code must be 1-128 ASCII letters, digits, '_' or '-'",
    "INVALID_KB_NAME": "kb name must be 1-255 characters",
    "KB_CODE_EXISTS": "kb code already exists",
    "EMPTY_KB_UPDATE": "specify --name or --description",
    "INVALID_MANAGEMENT_STATUS": "invalid kind or management status",
    "INVALID_RESOURCE": "unknown management resource type",
    "INVALID_STATUS_FILTER": "invalid status for this resource",
    "UNSUPPORTED_STATUS_FILTER": "this resource does not support status filtering",
    "INVALID_PAGE": "require 1 <= limit <= 1000 and offset >= 0",
    "RECORD_NOT_FOUND": "requested management record does not exist",
    "BUILD_SELECTOR_REQUIRED": "provide --document-id or --build-id",
    "BUILD_OWNERSHIP": "build does not belong to the requested document",
    "RESOURCE_DISABLED": "knowledge base/document is disabled or archived",
    "NO_ACTIVE_BUILD": "document has no active build; explicitly activate one first",
    "BUILD_NOT_READY": "only ready, nonempty builds may be activated",
    "BUILD_INCOMPLETE": "build chunk count is incomplete",
    "IMPORT_NOT_COMPLETE": "only a fully succeeded import can be batch-activated",
    "IMPORT_ITEM_INCOMPLETE": "import item count or success mapping is incomplete",
    "IMPORT_OWNERSHIP": "import result belongs to another knowledge base",
    "INVALID_PATH": "source/asset path must be a nonempty relative path",
    "UNSAFE_PATH": "unsafe relative path",
    "PATH_TOO_LONG": "relative path is empty or too long",
    "PATH_ESCAPE": "path escapes storage/input root",
    "INVALID_DOC_ID": "invalid doc_id",
    "INVALID_CHUNK_ID": "invalid chunk_id",
    "INVALID_TOKEN_COUNT": "invalid token_count",
    "INVALID_CHUNK_INDEX": "invalid chunk_index",
    "INVALID_FIELD": "invalid corpus field",
    "STORED_SNAPSHOT_CHECKSUM": "immutable snapshot checksum mismatch",
    "MANIFEST_REQUIRED": "corpus requires manifest.json or report.json with artifact hashes",
    "SNAPSHOT_CHECKSUM": "snapshot checksum mismatch",
    "EMPTY_CORPUS": "corpus contains no documents",
    "DUPLICATE_DOCUMENT": "duplicate document ID or normalized source path",
    "INVALID_SOURCE_HASH": "invalid document sha256",
    "INVALID_DOCUMENT_FORMAT": "document format must match a PDF/DOCX source path",
    "INVALID_DOCUMENT_STATUS": "invalid document status",
    "MISSING_BLOCKS": "document blocks are required for source provenance",
    "DUPLICATE_BLOCK": "duplicate block ID",
    "DUPLICATE_CHUNK": "duplicate chunk ID",
    "ORPHAN_CHUNK": "orphan chunk references an unknown document",
    "CHUNK_SOURCE_MISMATCH": "chunk source path differs from document",
    "INVALID_RETRIEVAL_FLAG": "invalid retrieval_enabled",
    "INVALID_CHUNK_BODY": "missing/nonempty chunk body",
    "INVALID_CHUNK_TEXT": "missing/nonempty chunk text",
    "INVALID_STRUCTURE": "invalid chunk structure_type/content_type",
    "DUPLICATE_CHUNK_POSITION": "duplicate chunk_index",
    "MISSING_PROVENANCE": "chunk source provenance is required",
    "UNKNOWN_SOURCE_BLOCK": "source provenance references an unknown block",
    "INVALID_SOURCE_INTERVAL": "invalid source character interval",
    "CHUNK_COUNT_MISMATCH": "document chunk_count mismatch",
    "FAILED_DOCUMENT_CHUNKS": "failed/empty document must not contain chunks",
    "MISSING_DECLARED_ASSET": "declared parser artifact missing",
    "DERIVED_ASSET_CHECKSUM": "derived artifact checksum mismatch",
    "ASSET_CHECKSUM": "parser artifact checksum mismatch",
    "RAW_FILE_MISSING": "raw file missing",
    "RAW_CHECKSUM": "raw file checksum mismatch",
    "STORED_RAW_CHECKSUM": "immutable raw file checksum mismatch",
    "MYSQL_PASSWORD_REQUIRED": "MYSQL_PASSWORD is required (environment or .env.mysql)",
    "INVALID_DATABASE_NAME": "database name must be a simple SQL identifier",
    "MYSQL_COMMAND_FAILED": "Docker MySQL command failed; verify the container and database permissions",
    "BACKUP_EXISTS": "backup output already exists; choose a new directory",
    "BACKUP_INSIDE_STORAGE": "backup output must be outside the managed storage root",
    "BACKUP_DUMP_FAILED": "mysqldump failed; backup was not published",
    "RESTORE_NOT_ISOLATED": "restore requires a different database and a new storage directory",
    "BACKUP_CHECKSUM": "backup checksum mismatch",
    "RESTORE_DATABASE_EXISTS": "restore target database already exists",
    "RESTORE_FAILED": "database restore failed; inspect the isolated target before retrying",
    "RESTORE_PATH_ESCAPE": "backup contains a storage reference outside its recorded root",
    "LEGACY_ERROR": "historical error details are withheld",
    "DATABASE_ERROR": "database operation failed",
    "FILE_SYSTEM_ERROR": "file/system operation failed",
    "INVALID_JSON": "invalid JSON input",
    "INVALID_ENCODING": "invalid text encoding",
    "INTERNAL_ERROR": "unexpected management operation failure",
}


class ManagementError(ValueError):
    def __init__(self, code):
        self.code = code if code in MESSAGES else "INTERNAL_ERROR"
        super().__init__(MESSAGES[self.code])


def public_error(exc):
    """Never stringify untrusted exceptions, including ordinary ValueError."""
    if isinstance(exc, ManagementError):
        code = exc.code if exc.code in MESSAGES else "INTERNAL_ERROR"
        return {"error_code": code, "error": MESSAGES[code]}
    if isinstance(exc, SQLAlchemyError):
        args = getattr(getattr(exc, "orig", None), "args", ())
        number = args[0] if args and type(args[0]) is int else None
        message = "database operation failed"
        if number is not None:
            message += f" (code={number})"
        return {"error_code": "DATABASE_ERROR", "error": message}
    if isinstance(exc, json.JSONDecodeError):
        return {"error_code": "INVALID_JSON", "error": "invalid JSON input"}
    if isinstance(exc, UnicodeError):
        return {"error_code": "INVALID_ENCODING", "error": "invalid text encoding"}
    if isinstance(exc, OSError):
        message = "file/system operation failed"
        if type(exc.errno) is int:
            message += f" (errno={exc.errno})"
        return {"error_code": "FILE_SYSTEM_ERROR", "error": message}
    return {"error_code": "INTERNAL_ERROR", "error": MESSAGES["INTERNAL_ERROR"]}


def safe_error(exc):
    return public_error(exc)["error"]


def stored_error(code, message):
    """Do not trust historical error strings or manually changed error columns."""
    code = code if code in MESSAGES else "LEGACY_ERROR"
    public = MESSAGES[code]
    patterns = {"DATABASE_ERROR": r"database operation failed \(code=-?\d+\)",
                "FILE_SYSTEM_ERROR": r"file/system operation failed \(errno=-?\d+\)"}
    if code in patterns and isinstance(message, str) and re.fullmatch(patterns[code], message):
        public = message
    return {"error_code": code, "error": public}
