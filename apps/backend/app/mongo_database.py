"""MongoDB data layer for Resume Matcher (optional persistent store).

``MongoDatabase`` mirrors the ``app.database.Database`` facade 1:1 — same
method names, signatures, and plain-dict contracts — so routers and services
work unchanged regardless of backend. It is selected automatically when
``MONGODB_URI`` is set (see the ``db`` factory at the bottom of
``app/database.py``); otherwise the local SQLite database is used.

Design notes:

- Async document operations use **Motor**; the encrypted ``api_keys`` store is
  read on the synchronous LLM hot path, so it uses a sync **pymongo** client
  (same split as the SQLite backend's async/sync engines).
- Each app-level string ID (``resume_id``, ``job_id``, …) is stored as the
  document ``_id`` for uniqueness and direct lookup. Converters strip ``_id``
  on read so callers never see it.
- No multi-document transactions: free/shared MongoDB tiers (e.g. Atlas M0)
  don't guarantee them. Compound operations use ordered sequential writes plus
  conditional (compare-and-set) updates for ownership hand-off
  (``claim_*``/``complete_preview``/``finish_resume_processing``). The
  single-master and (job, resume) dedupe invariants are enforced by unique
  indexes instead.
- Job documents store dynamic pipeline fields flat at top level — no
  ``metadata_json`` split is needed on a document store — while ``get_job``
  still returns the same flattened dict the SQLite backend produces.
"""

import copy
import logging
import shutil
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import MongoClient, ReturnDocument
from pymongo.database import Database as SyncDatabase
from pymongo.errors import DuplicateKeyError

from app.config import settings
from app.database import ProcessingFinishOutcome, ResumeNotFoundError
from app.preview import (
    PreviewBusyError,
    PreviewClaim,
    PreviewConflictError,
    PreviewValidationError,
    job_fingerprint,
    resume_fingerprint,
)

logger = logging.getLogger(__name__)

# Resume fields accepted by update_resume (mirrors the SQLite column check,
# minus identity fields: _id/resume_id must never change after creation).
_RESUME_MUTABLE_FIELDS = frozenset(
    {
        "content",
        "content_type",
        "filename",
        "is_master",
        "parent_id",
        "processed_data",
        "processing_status",
        "processing_token",
        "cover_letter",
        "outreach_message",
        "interview_prep",
        "title",
        "original_markdown",
        "created_at",
        "updated_at",
    }
)

# Fail fast when the URI is unreachable instead of hanging on server selection.
_MONGO_CLIENT_KWARGS: dict[str, Any] = {"serverSelectionTimeoutMS": 5000}


def _now() -> str:
    """Current UTC time as an ISO-8601 string (TinyDB-era format)."""
    return datetime.now(timezone.utc).isoformat()


class MongoDatabase:
    """Async Motor facade for resume matcher data (MongoDB backend)."""

    def __init__(
        self,
        uri: str | None = None,
        database_name: str | None = None,
        *,
        async_client: AsyncIOMotorClient | None = None,
        sync_client: MongoClient | None = None,
        create_indexes: bool = True,
    ):
        """Create the facade; clients connect lazily on first use.

        Args:
            uri: MongoDB connection string (defaults to ``MONGODB_URI``).
            database_name: Database name (defaults to ``MONGODB_DATABASE``).
            async_client/sync_client: Pre-built clients (tests only — pass
                both or neither). Injected clients are never closed by
                :meth:`close`.
            create_indexes: Create unique/lookup indexes on first use.
        """
        if (async_client is None) != (sync_client is None):
            raise ValueError("async_client and sync_client must be passed together")
        self._uri = uri
        self._database_name = database_name
        self._async_client = async_client
        self._sync_client = sync_client
        self._owns_clients = async_client is None
        self._create_indexes = create_indexes
        self._async_db: AsyncIOMotorDatabase | None = None
        self._sync_db: SyncDatabase | None = None
        self._initialized = False

    # -- client / database plumbing -----------------------------------------

    def _ensure_initialized(self) -> None:
        """Create clients, database handles, and indexes once (idempotent).

        Index creation runs on the **sync** client so no event loop is needed;
        both clients point at the same database.
        """
        if self._initialized:
            return
        if self._async_client is None or self._sync_client is None:
            uri = self._uri or settings.mongodb_uri
            if not uri:
                raise RuntimeError(
                    "MONGODB_URI is not set; cannot use the MongoDB backend"
                )
            self._async_client = AsyncIOMotorClient(uri, **_MONGO_CLIENT_KWARGS)
            self._sync_client = MongoClient(uri, **_MONGO_CLIENT_KWARGS)
            self._owns_clients = True
        name = self._database_name or settings.mongodb_database
        self._async_db = self._async_client[name]
        self._sync_db = self._sync_client[name]
        if self._create_indexes:
            self._create_collection_indexes(self._sync_db)
        self._initialized = True

    @staticmethod
    def _create_collection_indexes(db: SyncDatabase) -> None:
        """Create unique/lookup indexes (idempotent; safe to re-run)."""
        # At most one master resume (partial unique index).
        db["resumes"].create_index(
            "is_master",
            unique=True,
            partialFilterExpression={"is_master": True},
        )
        db["resumes"].create_index("created_at")
        db["improvements"].create_index("tailored_resume_id")
        db["tailoring_previews"].create_index(
            [("source_id", 1), ("job_id", 1), ("payload_hash", 1), ("created_at", 1)]
        )
        db["tailoring_previews"].create_index("expires_at")
        db["tailoring_previews"].create_index("result_resume_id")
        db["tailoring_previews"].create_index("source_id")
        db["tailoring_previews"].create_index("job_id")
        # Concurrency-safe dedupe: a card is unique per (job, applied resume).
        db["applications"].create_index([("job_id", 1), ("resume_id", 1)], unique=True)
        db["applications"].create_index([("status", 1), ("position", 1)])

    @property
    def _db(self) -> AsyncIOMotorDatabase:
        self._ensure_initialized()
        assert self._async_db is not None
        return self._async_db

    @property
    def _sdb(self) -> SyncDatabase:
        self._ensure_initialized()
        assert self._sync_db is not None
        return self._sync_db

    async def close(self) -> None:
        """Close owned clients and drop handles (injected clients untouched)."""
        if self._owns_clients:
            if self._async_client is not None:
                self._async_client.close()
                self._async_client = None
            if self._sync_client is not None:
                self._sync_client.close()
                self._sync_client = None
        self._async_db = None
        self._sync_db = None
        self._initialized = False

    # -- document -> dict converters ----------------------------------------

    @staticmethod
    def _resume_to_dict(doc: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "resume_id": doc["resume_id"],
            "content": doc.get("content", ""),
            "content_type": doc.get("content_type", "md"),
            "filename": doc.get("filename"),
            "is_master": doc.get("is_master", False),
            "parent_id": doc.get("parent_id"),
            "processed_data": doc.get("processed_data"),
            "processing_status": doc.get("processing_status", "pending"),
            "cover_letter": doc.get("cover_letter"),
            "outreach_message": doc.get("outreach_message"),
            "interview_prep": doc.get("interview_prep"),
            "title": doc.get("title"),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
        }
        # Preserve TinyDB absence semantics: omit the key entirely when None.
        if doc.get("original_markdown") is not None:
            result["original_markdown"] = doc["original_markdown"]
        return result

    @staticmethod
    def _job_to_dict(doc: dict[str, Any]) -> dict[str, Any]:
        """Jobs are stored flat; strip only the internal ``_id``."""
        return {key: value for key, value in doc.items() if key != "_id"}

    @staticmethod
    def _improvement_to_dict(doc: dict[str, Any]) -> dict[str, Any]:
        return {
            "request_id": doc["request_id"],
            "original_resume_id": doc["original_resume_id"],
            "tailored_resume_id": doc["tailored_resume_id"],
            "job_id": doc["job_id"],
            "improvements": doc.get("improvements", []),
            "created_at": doc.get("created_at"),
        }

    @staticmethod
    def _application_to_dict(doc: dict[str, Any]) -> dict[str, Any]:
        return {
            "application_id": doc["application_id"],
            "job_id": doc["job_id"],
            "resume_id": doc["resume_id"],
            "master_resume_id": doc.get("master_resume_id"),
            "status": doc.get("status", "applied"),
            "company": doc.get("company"),
            "role": doc.get("role"),
            "applied_at": doc.get("applied_at"),
            "notes": doc.get("notes"),
            "position": doc.get("position", 0),
            "created_at": doc.get("created_at"),
            "updated_at": doc.get("updated_at"),
        }

    # -- Resume operations --------------------------------------------------

    @staticmethod
    def _new_resume(**values: Any) -> dict[str, Any]:
        """Build a resume document for standalone or compound writes."""
        now = _now()
        resume_id = str(uuid4())
        return {
            "_id": resume_id,
            "resume_id": resume_id,
            "created_at": now,
            "updated_at": now,
            **values,
        }

    async def create_resume(
        self,
        content: str,
        content_type: str = "md",
        filename: str | None = None,
        is_master: bool = False,
        parent_id: str | None = None,
        processed_data: dict[str, Any] | None = None,
        processing_status: str = "pending",
        cover_letter: str | None = None,
        outreach_message: str | None = None,
        title: str | None = None,
        original_markdown: str | None = None,
        interview_prep: str | None = None,
    ) -> dict[str, Any]:
        """Create a new resume entry.

        processing_status: "pending", "processing", "ready", "failed"
        """
        doc = self._new_resume(
            content=content,
            content_type=content_type,
            filename=filename,
            is_master=is_master,
            parent_id=parent_id,
            processed_data=processed_data,
            processing_status=processing_status,
            processing_token=None,
            cover_letter=cover_letter,
            outreach_message=outreach_message,
            interview_prep=interview_prep,
            title=title,
            original_markdown=original_markdown,
        )
        await self._db["resumes"].insert_one(doc)
        return self._resume_to_dict(doc)

    async def create_resume_atomic_master(
        self,
        content: str,
        content_type: str = "md",
        filename: str | None = None,
        processed_data: dict[str, Any] | None = None,
        processing_status: str = "pending",
        cover_letter: str | None = None,
        outreach_message: str | None = None,
        original_markdown: str | None = None,
        title: str | None = None,
        interview_prep: str | None = None,
    ) -> dict[str, Any]:
        """Create a resume and replace a failed master in one flow."""
        resumes = self._db["resumes"]
        current_master = await resumes.find_one({"is_master": True})
        is_master = current_master is None
        if current_master and current_master.get("processing_status") in (
            "failed",
            "processing",
        ):
            # Demote first so the partial unique index slot is free; a later
            # insertion failure leaves no master, matching the SQLite
            # rollback outcome (no master row promoted).
            await resumes.update_one(
                {"_id": current_master["_id"]}, {"$set": {"is_master": False}}
            )
            is_master = True
        doc = self._new_resume(
            content=content,
            content_type=content_type,
            filename=filename,
            is_master=is_master,
            processed_data=processed_data,
            processing_status=processing_status,
            processing_token=None,
            cover_letter=cover_letter,
            outreach_message=outreach_message,
            interview_prep=interview_prep,
            title=title,
            original_markdown=original_markdown,
        )
        await resumes.insert_one(doc)
        return self._resume_to_dict(doc)

    async def get_resume(self, resume_id: str) -> dict[str, Any] | None:
        """Get resume by ID."""
        doc = await self._db["resumes"].find_one({"_id": resume_id})
        return self._resume_to_dict(doc) if doc else None

    async def get_master_resume(self) -> dict[str, Any] | None:
        """Get the master resume if exists."""
        doc = await self._db["resumes"].find_one({"is_master": True})
        return self._resume_to_dict(doc) if doc else None

    async def update_resume(
        self, resume_id: str, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Update resume by ID.

        Raises:
            ResumeNotFoundError: If resume not found. It subclasses
                ``ValueError``, so existing ``except ValueError`` callers are
                unaffected.
        """
        sets = {
            key: value
            for key, value in updates.items()
            if key in _RESUME_MUTABLE_FIELDS
        }
        for key in updates:
            if key not in _RESUME_MUTABLE_FIELDS:
                logger.warning("Ignoring unknown resume field on update: %s", key)
        sets["updated_at"] = _now()
        result = await self._db["resumes"].update_one(
            {"_id": resume_id}, {"$set": sets}
        )
        if result.matched_count == 0:
            raise ResumeNotFoundError(resume_id)
        doc = await self._db["resumes"].find_one({"_id": resume_id})
        assert doc is not None  # Matched just above.
        return self._resume_to_dict(doc)

    async def claim_resume_processing(
        self,
        resume_id: str,
        *,
        allow_ready_at: str | None = None,
    ) -> str | None:
        """Rotate processing ownership and return its opaque operation token.

        Failed and processing rows are retryable. A legacy ready row is only
        claimable when the caller observed the same version and found its
        structured content empty. ``None`` means a concurrent completion made
        the row ineligible; a missing row raises ``ResumeNotFoundError``.
        """
        token = str(uuid4())
        or_filters: list[dict[str, Any]] = [
            {"processing_status": {"$in": ["failed", "processing"]}}
        ]
        if allow_ready_at is not None:
            or_filters.append(
                {"processing_status": "ready", "updated_at": allow_ready_at}
            )
        # Atomic compare-and-set: exactly one claimant wins the token.
        updated = await self._db["resumes"].find_one_and_update(
            {"_id": resume_id, "$or": or_filters},
            {
                "$set": {
                    "processing_status": "processing",
                    "processing_token": token,
                    "updated_at": _now(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            return token
        exists = await self._db["resumes"].find_one(
            {"_id": resume_id}, projection={"_id": 1}
        )
        if exists is None:
            raise ResumeNotFoundError(resume_id)
        return None

    async def finish_resume_processing(
        self,
        resume_id: str,
        token: str | None,
        *,
        processing_status: Literal["ready", "failed"],
        processed_data: dict[str, Any] | None = None,
    ) -> ProcessingFinishOutcome:
        """Finish an owned attempt, or retire an unclaimed row with ``None``."""
        if token is None and processing_status != "failed":
            raise ValueError("Ready processing requires an ownership token")
        values: dict[str, Any] = {
            "processing_status": processing_status,
            "processing_token": None,
            "updated_at": _now(),
            "processed_data": processed_data if processing_status == "ready" else None,
        }
        result = await self._db["resumes"].update_one(
            {
                "_id": resume_id,
                "processing_token": token,
                "processing_status": "processing",
            },
            {"$set": values},
        )
        if result.matched_count == 1:
            return "committed"
        exists = await self._db["resumes"].find_one(
            {"_id": resume_id}, projection={"_id": 1}
        )
        return "stale" if exists is not None else "missing"

    async def delete_resume(self, resume_id: str) -> bool:
        """Delete resume by ID."""
        resumes = self._db["resumes"]
        previews = self._db["tailoring_previews"]
        result = await resumes.delete_one({"_id": resume_id})
        if result.deleted_count == 0:
            return False
        # Keep a content-free consumed marker for deleted results so retries
        # cannot recreate them, while removing their cached personal data.
        await previews.update_many(
            {"result_resume_id": resume_id}, {"$set": {"response_data": None}}
        )
        await previews.delete_many({"source_id": resume_id})
        return True

    async def list_resumes(self) -> list[dict[str, Any]]:
        """List all resumes."""
        cursor = self._db["resumes"].find({}).sort("created_at", 1)
        return [self._resume_to_dict(doc) async for doc in cursor]

    async def set_master_resume(self, resume_id: str) -> bool:
        """Set a resume as the master, unsetting any existing master.

        Returns False if the resume doesn't exist. Demote-then-promote
        ordering keeps the partial unique index satisfied.
        """
        resumes = self._db["resumes"]
        target = await resumes.find_one({"_id": resume_id}, projection={"_id": 1})
        if target is None:
            logger.warning("Cannot set master: resume %s not found", resume_id)
            return False
        await resumes.update_many(
            {"is_master": True, "_id": {"$ne": resume_id}},
            {"$set": {"is_master": False}},
        )
        await resumes.update_one(
            {"_id": resume_id}, {"$set": {"is_master": True}}
        )
        return True

    # -- Job operations -----------------------------------------------------

    async def create_job(
        self, content: str, resume_id: str | None = None
    ) -> dict[str, Any]:
        """Create one job description using the atomic batch writer."""
        return (await self.create_jobs([content], resume_id))[0]

    async def create_jobs(
        self, contents: list[str], resume_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Persist a validated job-description batch, in input order."""
        if not contents:
            return []
        docs = [
            {
                "_id": (job_id := str(uuid4())),
                "job_id": job_id,
                "content": content,
                "resume_id": resume_id,
                "created_at": _now(),
            }
            for content in contents
        ]
        await self._db["jobs"].insert_many(docs)
        return [self._job_to_dict(doc) for doc in docs]

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Get job by ID (dynamic fields flattened to top level)."""
        doc = await self._db["jobs"].find_one({"_id": job_id})
        return self._job_to_dict(doc) if doc else None

    async def update_job(
        self, job_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update a job by ID.

        Documents are schemaless, so core and dynamic pipeline fields
        (``preview_hash``, ``job_keywords``, ``company``/``role``, …) are all
        set flat and round-trip through ``get_job`` as top-level keys.
        """
        sets = {
            key: value
            for key, value in updates.items()
            if key not in ("_id", "job_id")
        }
        result = await self._db["jobs"].update_one({"_id": job_id}, {"$set": sets})
        if result.matched_count == 0:
            return None
        doc = await self._db["jobs"].find_one({"_id": job_id})
        assert doc is not None  # Matched just above.
        return self._job_to_dict(doc)

    async def delete_job(self, job_id: str) -> bool:
        """Delete a job by ID (used to clean up an orphaned manual-add job)."""
        await self._db["tailoring_previews"].delete_many({"job_id": job_id})
        result = await self._db["jobs"].delete_one({"_id": job_id})
        return result.deleted_count > 0

    # -- Preview and confirmation operations --------------------------------

    async def _validate_preview_inputs(self, preview: dict[str, Any]) -> None:
        source = await self._db["resumes"].find_one({"_id": preview["source_id"]})
        job = await self._db["jobs"].find_one({"_id": preview["job_id"]})
        if (
            source is None
            or job is None
            or resume_fingerprint(
                source.get("content", ""),
                source.get("processed_data"),
                source.get("original_markdown"),
            )
            != preview["source_hash"]
            or job_fingerprint(job.get("content", "")) != preview["job_hash"]
        ):
            raise PreviewConflictError(
                "Resume or job description changed. Please retry preview."
            )

    async def register_preview(
        self,
        *,
        source_id: str,
        job_id: str,
        payload_hash: str,
        source_hash: str,
        job_hash: str,
        prompt_id: str,
        ttl_seconds: int,
        improvements: list[dict[str, Any]] | None = None,
    ) -> dict[str, str]:
        """Register the exact input/output snapshot before acknowledging preview."""
        now = _now()
        preview_id = str(uuid4())
        doc = {
            "_id": preview_id,
            "preview_id": preview_id,
            "improvements": copy.deepcopy(improvements or []),
            "source_id": source_id,
            "job_id": job_id,
            "payload_hash": payload_hash,
            "source_hash": source_hash,
            "job_hash": job_hash,
            "created_at": now,
            "expires_at": (
                datetime.fromisoformat(now) + timedelta(seconds=ttl_seconds)
            ).isoformat(),
            "result_resume_id": None,
            "claim_token": None,
            "claim_expires_at": None,
            "response_data": None,
        }
        await self._validate_preview_inputs(doc)
        # Sweep expired, uncommitted, unclaimed-or-lease-expired previews.
        # claim_token/claim_expires_at are always set/cleared together, so the
        # second branch only adds lease-expired claims (null expiries imply an
        # unclaimed row, already covered by the first branch).
        await self._db["tailoring_previews"].delete_many(
            {
                "expires_at": {"$lte": now},
                "result_resume_id": None,
                "$or": [
                    {"claim_token": None},
                    {"claim_expires_at": {"$lte": now}},
                ],
            }
        )
        job = await self._db["jobs"].find_one({"_id": job_id})
        assert job is not None  # Validated just above.
        hashes = dict(job.get("preview_hashes") or {})
        hashes[prompt_id] = payload_hash
        await self._db["jobs"].update_one(
            {"_id": job_id},
            {
                "$set": {
                    "preview_hash": payload_hash,
                    "preview_prompt_id": prompt_id,
                    "preview_hashes": hashes,
                }
            },
        )
        await self._db["tailoring_previews"].insert_one(doc)
        return {"preview_id": preview_id, "expires_at": doc["expires_at"]}

    async def claim_preview(
        self,
        *,
        preview_id: str | None,
        source_id: str,
        job_id: str,
        payload_hash: str,
        lease_seconds: int,
    ) -> PreviewClaim:
        """Claim once across workers; committed retries bypass generation."""
        previews = self._db["tailoring_previews"]
        if preview_id:
            row = await previews.find_one({"_id": preview_id})
        else:
            # Compatibility for clients that omit the new operation ID:
            # prefer the committed replay, else the newest unexpired preview.
            base = {
                "source_id": source_id,
                "job_id": job_id,
                "payload_hash": payload_hash,
            }
            row = await previews.find_one(
                {**base, "result_resume_id": {"$ne": None}},
                sort=[("created_at", -1)],
            )
            if row is None:
                row = await previews.find_one(
                    {**base, "expires_at": {"$gt": _now()}},
                    sort=[("created_at", -1)],
                )
        if row is None:
            raise PreviewValidationError(
                "Preview required before confirmation. Please retry preview."
            )
        if row["source_id"] != source_id or row["job_id"] != job_id:
            raise PreviewConflictError(
                "Preview belongs to different inputs. Please retry preview."
            )
        if row["payload_hash"] != payload_hash:
            raise PreviewValidationError(
                "Invalid improved resume data. Please retry preview."
            )
        if row.get("result_resume_id") is not None:
            if row.get("response_data") is None or await self._db[
                "resumes"
            ].find_one(
                {"_id": row["result_resume_id"]}, projection={"_id": 1}
            ) is None:
                raise PreviewConflictError(
                    "Confirmed resume was deleted. Please retry preview."
                )
            return PreviewClaim(
                row["preview_id"], response=copy.deepcopy(row["response_data"])
            )
        now = _now()
        if row["expires_at"] <= now:
            raise PreviewConflictError("Preview expired. Please retry preview.")
        await self._validate_preview_inputs(row)
        if (
            row.get("claim_token")
            and row.get("claim_expires_at")
            and row["claim_expires_at"] > now
        ):
            raise PreviewBusyError(
                "Confirmation is already in progress. Please retry shortly."
            )
        # Conditional claim: only an unclaimed-or-lease-expired, uncommitted
        # preview can take the token, so concurrent confirmers serialize.
        token = str(uuid4())
        claimed = await previews.update_one(
            {
                "_id": row["_id"],
                "result_resume_id": None,
                "$or": [
                    {"claim_token": None},
                    {"claim_expires_at": {"$lte": now}},
                ],
            },
            {
                "$set": {
                    "claim_token": token,
                    "claim_expires_at": (
                        datetime.fromisoformat(now)
                        + timedelta(seconds=lease_seconds)
                    ).isoformat(),
                }
            },
        )
        if claimed.matched_count != 1:
            # Lost a race: report the winner's state accurately.
            fresh = await previews.find_one({"_id": row["_id"]})
            if (
                fresh
                and fresh.get("result_resume_id") is not None
                and fresh.get("response_data") is not None
            ):
                return PreviewClaim(
                    fresh["preview_id"],
                    response=copy.deepcopy(fresh["response_data"]),
                )
            raise PreviewBusyError(
                "Confirmation is already in progress. Please retry shortly."
            )
        return PreviewClaim(
            row["preview_id"],
            token=token,
            improvements=copy.deepcopy(row.get("improvements") or []),
        )

    async def release_preview_claim(self, claim: PreviewClaim) -> None:
        """Release only this request's uncommitted claim, including on cancellation."""
        if not claim.token:
            return
        await self._db["tailoring_previews"].update_one(
            {"_id": claim.preview_id, "claim_token": claim.token},
            {"$set": {"claim_token": None, "claim_expires_at": None}},
        )

    async def complete_preview(
        self,
        *,
        claim: PreviewClaim,
        resume_fields: dict[str, Any],
        response_data: dict[str, Any],
        improvements: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Commit resume, required relation and replay snapshot."""
        previews = self._db["tailoring_previews"]
        preview = await previews.find_one({"_id": claim.preview_id})
        now = _now()
        if (
            preview is None
            or not claim.token
            or preview.get("claim_token") != claim.token
            or not preview.get("claim_expires_at")
            or preview["claim_expires_at"] <= now
        ):
            raise PreviewConflictError(
                "Confirmation ownership expired. Please retry preview."
            )
        await self._validate_preview_inputs(preview)
        doc = self._new_resume(**resume_fields)
        result = copy.deepcopy(response_data)
        result.update(
            resume_id=doc["resume_id"],
            preview_id=preview["preview_id"],
            preview_expires_at=preview["expires_at"],
        )
        await self._db["resumes"].insert_one(doc)
        await self._db["improvements"].insert_one(
            {
                "_id": result["request_id"],
                "request_id": result["request_id"],
                "original_resume_id": preview["source_id"],
                "tailored_resume_id": doc["resume_id"],
                "job_id": preview["job_id"],
                "improvements": improvements,
                "created_at": now,
            }
        )
        # Conditional finalize: the commit lands only if this claim still owns
        # the preview (a lost race surfaces as a conflict instead of a
        # double-commit).
        finalized = await previews.update_one(
            {
                "_id": preview["_id"],
                "claim_token": claim.token,
                "result_resume_id": None,
            },
            {
                "$set": {
                    "response_data": result,
                    "result_resume_id": doc["resume_id"],
                    "claim_token": None,
                    "claim_expires_at": None,
                }
            },
        )
        if finalized.matched_count != 1:
            raise PreviewConflictError(
                "Confirmation ownership expired. Please retry preview."
            )
        return result

    # -- Improvement operations ---------------------------------------------

    async def create_tailored_resume(
        self,
        *,
        request_id: str,
        original_resume_id: str,
        job_id: str,
        resume_fields: dict[str, Any],
        improvements: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Commit a direct tailoring result and its required relation together."""
        doc = self._new_resume(**resume_fields)
        now = _now()
        await self._db["resumes"].insert_one(doc)
        await self._db["improvements"].insert_one(
            {
                "_id": request_id,
                "request_id": request_id,
                "original_resume_id": original_resume_id,
                "tailored_resume_id": doc["resume_id"],
                "job_id": job_id,
                "improvements": copy.deepcopy(improvements),
                "created_at": now,
            }
        )
        return self._resume_to_dict(doc)

    async def create_improvement(
        self,
        original_resume_id: str,
        tailored_resume_id: str,
        job_id: str,
        improvements: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create an improvement result entry."""
        request_id = str(uuid4())
        now = _now()
        await self._db["improvements"].insert_one(
            {
                "_id": request_id,
                "request_id": request_id,
                "original_resume_id": original_resume_id,
                "tailored_resume_id": tailored_resume_id,
                "job_id": job_id,
                "improvements": improvements,
                "created_at": now,
            }
        )
        return {
            "request_id": request_id,
            "original_resume_id": original_resume_id,
            "tailored_resume_id": tailored_resume_id,
            "job_id": job_id,
            "improvements": improvements,
            "created_at": now,
        }

    async def get_improvement_by_tailored_resume(
        self, tailored_resume_id: str
    ) -> dict[str, Any] | None:
        """Get improvement record by tailored resume ID."""
        doc = await self._db["improvements"].find_one(
            {"tailored_resume_id": tailored_resume_id}
        )
        return self._improvement_to_dict(doc) if doc else None

    # -- Application (tracker) operations -----------------------------------

    async def _next_position(self, status: str) -> int:
        return await self._db["applications"].count_documents({"status": status})

    async def _renumber(self, status: str) -> None:
        """Renumber a column's positions to a contiguous 0..n-1 sequence."""
        applications = self._db["applications"]
        cursor = applications.find({"status": status}).sort(
            [("position", 1), ("created_at", 1)]
        )
        index = 0
        async for doc in cursor:
            if doc.get("position") != index:
                await applications.update_one(
                    {"_id": doc["_id"]}, {"$set": {"position": index}}
                )
            index += 1

    async def _build_application_doc(
        self,
        *,
        job_id: str,
        resume_id: str,
        master_resume_id: str | None = None,
        status: str = "applied",
        company: str | None = None,
        role: str | None = None,
        applied_at: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Build one card with shared date and ordering rules (not inserted)."""
        now = _now()
        if applied_at is None and status != "saved":
            applied_at = now
        position = await self._next_position(status)
        application_id = str(uuid4())
        return {
            "_id": application_id,
            "application_id": application_id,
            "job_id": job_id,
            "resume_id": resume_id,
            "master_resume_id": master_resume_id,
            "status": status,
            "company": company,
            "role": role,
            "applied_at": applied_at,
            "notes": notes,
            "position": position,
            "created_at": now,
            "updated_at": now,
        }

    async def _insert_application(
        self,
        *,
        job_id: str,
        resume_id: str,
        master_resume_id: str | None = None,
        status: str = "applied",
        company: str | None = None,
        role: str | None = None,
        applied_at: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Build and insert one card with shared date and ordering rules."""
        doc = await self._build_application_doc(
            job_id=job_id,
            resume_id=resume_id,
            master_resume_id=master_resume_id,
            status=status,
            company=company,
            role=role,
            applied_at=applied_at,
            notes=notes,
        )
        await self._db["applications"].insert_one(doc)
        return doc

    async def create_manual_application(
        self,
        *,
        content: str,
        resume_id: str,
        status: str = "applied",
        company: str | None = None,
        role: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Commit a pasted job and its tracker card together."""
        job_id = str(uuid4())
        job_doc: dict[str, Any] = {
            "_id": job_id,
            "job_id": job_id,
            "content": content,
            "resume_id": resume_id,
            "created_at": _now(),
        }
        if company or role:
            job_doc["company"] = company
            job_doc["role"] = role
        await self._db["jobs"].insert_one(job_doc)
        doc = await self._insert_application(
            job_id=job_id,
            resume_id=resume_id,
            status=status,
            company=company,
            role=role,
            notes=notes,
        )
        return self._application_to_dict(doc)

    async def create_application(
        self,
        job_id: str,
        resume_id: str,
        master_resume_id: str | None = None,
        status: str = "applied",
        company: str | None = None,
        role: str | None = None,
        applied_at: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Create a tracker card, deduped on (job_id, resume_id).

        If a card for the same job+resume already exists it is returned as-is
        (survives double-submit / retried confirms).
        """
        applications = self._db["applications"]
        # A replay needs only a read.
        found = await applications.find_one(
            {"job_id": job_id, "resume_id": resume_id}
        )
        if found is not None:
            return self._application_to_dict(found)
        doc = await self._build_application_doc(
            job_id=job_id,
            resume_id=resume_id,
            master_resume_id=master_resume_id,
            status=status,
            company=company,
            role=role,
            applied_at=applied_at,
            notes=notes,
        )
        try:
            await applications.insert_one(doc)
        except DuplicateKeyError:
            # A concurrent create won the (job_id, resume_id) unique
            # constraint — return the existing card instead of duplicating.
            dup = await applications.find_one(
                {"job_id": job_id, "resume_id": resume_id}
            )
            if dup is not None:
                logger.debug(
                    "Deduped concurrent application create for job=%s resume=%s",
                    job_id,
                    resume_id,
                )
                return self._application_to_dict(dup)
            raise
        return self._application_to_dict(doc)

    async def list_applications(
        self, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List applications ordered by (status, position)."""
        query: dict[str, Any] = {"status": status} if status is not None else {}
        cursor = (
            self._db["applications"].find(query).sort([("status", 1), ("position", 1)])
        )
        return [self._application_to_dict(doc) async for doc in cursor]

    async def get_application(
        self, application_id: str
    ) -> dict[str, Any] | None:
        """Get an application by ID."""
        doc = await self._db["applications"].find_one({"_id": application_id})
        return self._application_to_dict(doc) if doc else None

    async def update_application(
        self, application_id: str, updates: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update an application; renumber columns when status/position change.

        ``position`` is interpreted as the desired index within the (possibly
        new) ``status`` column; siblings are renumbered server-side so the
        column stays a contiguous 0..n-1 sequence.
        """
        applications = self._db["applications"]
        row = await applications.find_one({"_id": application_id})
        if row is None:
            return None

        old_status = row.get("status", "applied")
        new_status = updates.get("status", old_status)
        target_position = updates.get("position", None)

        sets: dict[str, Any] = {}
        for key in ("company", "role", "applied_at", "notes"):
            if key in updates:
                sets[key] = updates[key]

        if (
            old_status == "saved"
            and new_status != "saved"
            and row.get("applied_at") is None
            and "applied_at" not in updates
        ):
            sets["applied_at"] = _now()

        moved = "status" in updates or "position" in updates
        if moved:
            # Park it out of the way, renumber both columns, then reinsert.
            sets.update(status=new_status, position=10_000_000)
            await applications.update_one({"_id": application_id}, {"$set": sets})
            if old_status != new_status:
                await self._renumber(old_status)
            # Renumber the target column excluding this row, then splice in.
            ordered = await (
                applications.find(
                    {"status": new_status, "_id": {"$ne": application_id}}
                )
                .sort([("position", 1), ("created_at", 1)])
                .to_list(length=None)
            )
            if target_position is None or target_position > len(ordered):
                target_position = len(ordered)
            if target_position < 0:
                target_position = 0
            for index, item in enumerate(ordered):
                final = index if index < target_position else index + 1
                if item.get("position") != final:
                    await applications.update_one(
                        {"_id": item["_id"]}, {"$set": {"position": final}}
                    )
            await applications.update_one(
                {"_id": application_id},
                {"$set": {"position": target_position, "updated_at": _now()}},
            )
        else:
            sets["updated_at"] = _now()
            await applications.update_one({"_id": application_id}, {"$set": sets})

        doc = await applications.find_one({"_id": application_id})
        assert doc is not None  # Existed just above.
        return self._application_to_dict(doc)

    async def bulk_update_applications(
        self, application_ids: list[str], status: str
    ) -> int:
        """Move many applications to the end of ``status``. Returns count moved."""
        applications = self._db["applications"]
        moved = 0
        affected_old: set[str] = set()
        for application_id in application_ids:
            row = await applications.find_one({"_id": application_id})
            if row is None:
                continue
            affected_old.add(row.get("status", "applied"))
            sets: dict[str, Any] = {
                "status": status,
                "position": 20_000_000 + moved,  # provisional, renumbered below
                "updated_at": _now(),
            }
            if row.get("status") == "saved" and status != "saved" and row.get(
                "applied_at"
            ) is None:
                sets["applied_at"] = _now()
            await applications.update_one({"_id": application_id}, {"$set": sets})
            moved += 1
        for old_status in affected_old - {status}:
            await self._renumber(old_status)
        await self._renumber(status)
        return moved

    async def delete_application(self, application_id: str) -> bool:
        """Delete an application; renumber its column."""
        applications = self._db["applications"]
        row = await applications.find_one({"_id": application_id})
        if row is None:
            return False
        status = row.get("status", "applied")
        await applications.delete_one({"_id": application_id})
        await self._renumber(status)
        return True

    async def bulk_delete_applications(self, application_ids: list[str]) -> int:
        """Delete many applications; renumber affected columns. Returns count."""
        applications = self._db["applications"]
        deleted = 0
        affected: set[str] = set()
        for application_id in application_ids:
            row = await applications.find_one({"_id": application_id})
            if row is None:
                continue
            affected.add(row.get("status", "applied"))
            await applications.delete_one({"_id": application_id})
            deleted += 1
        for status in affected:
            await self._renumber(status)
        return deleted

    # -- Encrypted API key store (sync; read on the LLM hot path) -----------

    def get_api_key_ciphertexts(self) -> dict[str, str]:
        """Return ``{provider: ciphertext}`` for all stored keys (sync)."""
        return {
            doc["_id"]: doc["ciphertext"]
            for doc in self._sdb["api_keys"].find({}, {"ciphertext": 1})
        }

    def set_api_key_ciphertext(self, provider: str, ciphertext: str) -> None:
        """Upsert one provider's ciphertext (sync)."""
        self._sdb["api_keys"].update_one(
            {"_id": provider},
            {
                "$set": {
                    "provider": provider,
                    "ciphertext": ciphertext,
                    "updated_at": _now(),
                }
            },
            upsert=True,
        )

    def delete_api_key(self, provider: str) -> None:
        """Delete one provider's key (sync)."""
        self._sdb["api_keys"].delete_one({"_id": provider})

    def clear_api_keys(self) -> None:
        """Delete all stored keys (sync)."""
        self._sdb["api_keys"].delete_many({})

    def replace_api_keys(self, ciphertexts: dict[str, str]) -> None:
        """Replace the whole key store (clear + insert back-to-back).

        Best-effort ordering instead of one transaction (free/shared tiers
        don't guarantee multi-document transactions); callers encrypt
        everything up front so a mid-write failure can't corrupt entries —
        at worst the store is left empty and the user re-saves keys.
        """
        self._sdb["api_keys"].delete_many({})
        now = _now()
        docs = [
            {
                "_id": provider,
                "provider": provider,
                "ciphertext": ciphertext,
                "updated_at": now,
            }
            for provider, ciphertext in ciphertexts.items()
            if ciphertext
        ]
        if docs:
            self._sdb["api_keys"].insert_many(docs)

    # -- Stats / maintenance ------------------------------------------------

    async def get_stats(self) -> dict[str, Any]:
        """Get database statistics."""
        resumes = await self._db["resumes"].count_documents({})
        jobs = await self._db["jobs"].count_documents({})
        improvements = await self._db["improvements"].count_documents({})
        master = await self._db["resumes"].find_one(
            {"is_master": True}, projection={"_id": 1}
        )
        return {
            "total_resumes": int(resumes or 0),
            "total_jobs": int(jobs or 0),
            "total_improvements": int(improvements or 0),
            "has_master_resume": master is not None,
        }

    async def reset_database(self) -> None:
        """Reset by truncating user-document tables and clearing uploads.

        Clears resumes/jobs/improvements, preview replay data, and tracker
        applications (leaving orphaned cards after a full data reset would be
        a bug). Encrypted ``api_keys`` are preserved — matching the SQLite
        backend where a reset never wiped the user's stored credentials.
        """
        db = self._db
        await db["tailoring_previews"].delete_many({})
        await db["applications"].delete_many({})
        await db["improvements"].delete_many({})
        await db["jobs"].delete_many({})
        await db["resumes"].delete_many({})

        uploads_dir = settings.data_dir / "uploads"
        if uploads_dir.exists():
            shutil.rmtree(uploads_dir)
            uploads_dir.mkdir(parents=True, exist_ok=True)
