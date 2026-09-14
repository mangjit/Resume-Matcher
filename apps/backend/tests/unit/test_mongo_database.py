"""Tests for the MongoDB layer (app.mongo_database.MongoDatabase).

Runs against in-process mocks (mongomock + mongomock-motor), so no server or
network is needed. The sync/async mock clients are separate stores, which is
fine because only the api_keys collection is touched synchronously.

Index enforcement is disabled in the functional fixture (indexes are created
on the sync store only, while the async mock holds the documents); index
specs are verified separately in TestIndexes.
"""

import pytest
from mongomock_motor import AsyncMongoMockClient
from pymongo.errors import DuplicateKeyError

import mongomock
from app.config import Settings, settings
from app.database import Database, _build_default_database
from app.mongo_database import MongoDatabase
from app.preview import (
    PreviewBusyError,
    PreviewConflictError,
    PreviewValidationError,
    job_fingerprint,
    resume_fingerprint,
)


@pytest.fixture
def mongo_clients():
    return AsyncMongoMockClient(), mongomock.MongoClient()


@pytest.fixture
async def mdb(mongo_clients):
    async_client, sync_client = mongo_clients
    database = MongoDatabase(
        async_client=async_client, sync_client=sync_client, create_indexes=False
    )
    yield database
    await database.close()


@pytest.fixture
def stored_job_and_resume(mdb):
    """Persist one resume + job and return their fingerprints for previews."""

    async def _make(**resume_kwargs):
        resume = await mdb.create_resume(content="# Resume", **resume_kwargs)
        job = await mdb.create_job(content="Build things with Python")
        return resume, job

    return _make


def _hashes(resume, job):
    return (
        resume_fingerprint(
            resume["content"], resume["processed_data"], resume.get("original_markdown")
        ),
        job_fingerprint(job["content"]),
    )


class TestBackendSelection:
    def test_default_is_sqlite_when_uri_unset(self, monkeypatch):
        monkeypatch.setattr(settings, "mongodb_uri", None)
        assert isinstance(_build_default_database(), Database)

    def test_mongodb_selected_when_uri_set(self, monkeypatch):
        monkeypatch.setattr(settings, "mongodb_uri", "mongodb://localhost:27017/x")
        assert isinstance(_build_default_database(), MongoDatabase)

    def test_blank_uri_means_sqlite(self):
        assert Settings(mongodb_uri="").mongodb_uri is None
        assert Settings(mongodb_uri="   ").mongodb_uri is None

    def test_invalid_uri_scheme_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            Settings(mongodb_uri="http://localhost:27017")

    def test_valid_uris_accepted(self):
        assert (
            Settings(mongodb_uri="mongodb://h:27017").mongodb_uri
            == "mongodb://h:27017"
        )
        assert (
            Settings(mongodb_uri="mongodb+srv://u:p@cluster/x").mongodb_uri
            == "mongodb+srv://u:p@cluster/x"
        )

    def test_mismatched_client_injection_rejected(self, mongo_clients):
        async_client, _ = mongo_clients
        with pytest.raises(ValueError):
            MongoDatabase(async_client=async_client)
        with pytest.raises(ValueError):
            MongoDatabase(sync_client=mongomock.MongoClient())


class TestResumeCrud:
    async def test_create_and_get(self, mdb):
        created = await mdb.create_resume(content="# Resume", filename="r.pdf")
        assert created["resume_id"]
        assert "_id" not in created
        assert "processing_token" not in created
        fetched = await mdb.get_resume(created["resume_id"])
        assert fetched is not None
        assert fetched["content"] == "# Resume"
        assert fetched["filename"] == "r.pdf"

    async def test_get_missing_returns_none(self, mdb):
        assert await mdb.get_resume("does-not-exist") is None

    async def test_list_resumes_ordered_by_created_at(self, mdb):
        first = await mdb.create_resume(content="a")
        second = await mdb.create_resume(content="b")
        listed = await mdb.list_resumes()
        assert [doc["resume_id"] for doc in listed] == [
            first["resume_id"],
            second["resume_id"],
        ]

    async def test_update_resume_changes_field_and_timestamp(self, mdb):
        created = await mdb.create_resume(content="x")
        updated = await mdb.update_resume(created["resume_id"], {"title": "New Title"})
        assert updated["title"] == "New Title"
        assert updated["updated_at"] >= created["updated_at"]

    async def test_update_ignores_unknown_fields(self, mdb):
        created = await mdb.create_resume(content="x")
        updated = await mdb.update_resume(
            created["resume_id"], {"title": "T", "nope": "ignored"}
        )
        assert updated["title"] == "T"
        assert "nope" not in updated

    async def test_update_missing_raises(self, mdb):
        with pytest.raises(ValueError):
            await mdb.update_resume("missing", {"title": "X"})

    async def test_delete_resume(self, mdb):
        created = await mdb.create_resume(content="x")
        assert await mdb.delete_resume(created["resume_id"]) is True
        assert await mdb.get_resume(created["resume_id"]) is None

    async def test_delete_missing_returns_false(self, mdb):
        assert await mdb.delete_resume("missing") is False

    async def test_original_markdown_absence_semantics(self, mdb):
        without = await mdb.create_resume(content="x")
        assert "original_markdown" not in without
        with_md = await mdb.create_resume(content="x", original_markdown="# md")
        assert with_md["original_markdown"] == "# md"
        fetched = await mdb.get_resume(with_md["resume_id"])
        assert fetched is not None
        assert fetched["original_markdown"] == "# md"

    async def test_set_master_demotes_previous(self, mdb):
        first = await mdb.create_resume(content="a")
        second = await mdb.create_resume(content="b")
        assert await mdb.set_master_resume(first["resume_id"]) is True
        assert (await mdb.get_master_resume())["resume_id"] == first["resume_id"]
        assert await mdb.set_master_resume(second["resume_id"]) is True
        assert (await mdb.get_master_resume())["resume_id"] == second["resume_id"]
        assert (await mdb.get_resume(first["resume_id"]))["is_master"] is False

    async def test_set_master_missing_returns_false(self, mdb):
        assert await mdb.set_master_resume("missing") is False

    async def test_create_resume_atomic_master(self, mdb):
        first = await mdb.create_resume_atomic_master(content="a")
        assert first["is_master"] is True
        second = await mdb.create_resume_atomic_master(content="b")
        assert second["is_master"] is False
        # A failed master is replaced atomically.
        await mdb.update_resume(first["resume_id"], {"processing_status": "failed"})
        third = await mdb.create_resume_atomic_master(content="c")
        assert third["is_master"] is True
        assert (await mdb.get_resume(first["resume_id"]))["is_master"] is False


class TestProcessingOwnership:
    async def test_claim_and_finish_ready(self, mdb):
        created = await mdb.create_resume(
            content="x", processing_status="failed", processed_data={"a": 1}
        )
        token = await mdb.claim_resume_processing(created["resume_id"])
        assert token
        outcome = await mdb.finish_resume_processing(
            created["resume_id"],
            token,
            processing_status="ready",
            processed_data={"structured": True},
        )
        assert outcome == "committed"
        fetched = await mdb.get_resume(created["resume_id"])
        assert fetched is not None
        assert fetched["processing_status"] == "ready"
        assert fetched["processed_data"] == {"structured": True}

    async def test_finish_with_wrong_token_is_stale(self, mdb):
        created = await mdb.create_resume(content="x", processing_status="failed")
        token = await mdb.claim_resume_processing(created["resume_id"])
        assert token
        assert (
            await mdb.finish_resume_processing(
                created["resume_id"], "wrong-token", processing_status="failed"
            )
            == "stale"
        )

    async def test_finish_missing_row(self, mdb):
        assert (
            await mdb.finish_resume_processing(
                "missing", "token", processing_status="failed"
            )
            == "missing"
        )

    async def test_ready_requires_token(self, mdb):
        created = await mdb.create_resume(content="x")
        with pytest.raises(ValueError):
            await mdb.finish_resume_processing(
                created["resume_id"], None, processing_status="ready"
            )

    async def test_claim_missing_raises(self, mdb):
        from app.database import ResumeNotFoundError

        with pytest.raises(ResumeNotFoundError):
            await mdb.claim_resume_processing("missing")

    async def test_claim_ready_row_needs_matching_version(self, mdb):
        created = await mdb.create_resume(content="x", processing_status="ready")
        assert await mdb.claim_resume_processing(created["resume_id"]) is None
        assert (
            await mdb.claim_resume_processing(created["resume_id"], allow_ready_at="old")
            is None
        )
        token = await mdb.claim_resume_processing(
            created["resume_id"], allow_ready_at=created["updated_at"]
        )
        assert token


class TestJobs:
    async def test_crud(self, mdb):
        created = await mdb.create_job(content="job", resume_id="r1")
        assert created["job_id"]
        assert "_id" not in created
        fetched = await mdb.get_job(created["job_id"])
        assert fetched is not None
        assert fetched["content"] == "job"
        assert fetched["resume_id"] == "r1"

    async def test_dynamic_fields_round_trip(self, mdb):
        created = await mdb.create_job(content="job")
        updated = await mdb.update_job(
            created["job_id"],
            {"preview_hash": "abc", "job_keywords": {"a": 1}, "company": "Acme"},
        )
        assert updated is not None
        assert updated["preview_hash"] == "abc"
        assert updated["job_keywords"] == {"a": 1}
        fetched = await mdb.get_job(created["job_id"])
        assert fetched is not None
        assert fetched["company"] == "Acme"

    async def test_update_missing_returns_none(self, mdb):
        assert await mdb.update_job("missing", {"a": 1}) is None

    async def test_delete_job(self, mdb):
        created = await mdb.create_job(content="job")
        assert await mdb.delete_job(created["job_id"]) is True
        assert await mdb.delete_job(created["job_id"]) is False

    async def test_create_jobs_preserves_order(self, mdb):
        batch = await mdb.create_jobs(["one", "two", "three"])
        assert [job["content"] for job in batch] == ["one", "two", "three"]

    async def test_create_jobs_empty_batch(self, mdb):
        assert await mdb.create_jobs([]) == []


class TestPreviews:
    async def _register(self, mdb, resume, job, **kwargs):
        source_hash, job_hash = _hashes(resume, job)
        params = {
            "source_id": resume["resume_id"],
            "job_id": job["job_id"],
            "payload_hash": "payload-1",
            "source_hash": source_hash,
            "job_hash": job_hash,
            "prompt_id": "tailor",
            "ttl_seconds": 3600,
        }
        params.update(kwargs)
        return await mdb.register_preview(**params)

    async def test_register_updates_job_preview_fields(self, mdb, stored_job_and_resume):
        resume, job = await stored_job_and_resume()
        registered = await self._register(mdb, resume, job)
        assert registered["preview_id"]
        assert registered["expires_at"]
        fetched = await mdb.get_job(job["job_id"])
        assert fetched is not None
        assert fetched["preview_hash"] == "payload-1"
        assert fetched["preview_prompt_id"] == "tailor"
        assert fetched["preview_hashes"] == {"tailor": "payload-1"}

    async def test_claim_complete_replay(self, mdb, stored_job_and_resume):
        resume, job = await stored_job_and_resume()
        registered = await self._register(
            mdb, resume, job, improvements=[{"path": "summary"}]
        )
        claim = await mdb.claim_preview(
            preview_id=registered["preview_id"],
            source_id=resume["resume_id"],
            job_id=job["job_id"],
            payload_hash="payload-1",
            lease_seconds=60,
        )
        assert claim.token
        assert claim.improvements == [{"path": "summary"}]
        # A second claim while owned is busy.
        with pytest.raises(PreviewBusyError):
            await mdb.claim_preview(
                preview_id=registered["preview_id"],
                source_id=resume["resume_id"],
                job_id=job["job_id"],
                payload_hash="payload-1",
                lease_seconds=60,
            )
        result = await mdb.complete_preview(
            claim=claim,
            resume_fields={"content": "tailored", "parent_id": resume["resume_id"]},
            response_data={"request_id": "req-1", "resume": "tailored"},
            improvements=[{"path": "summary"}],
        )
        assert result["request_id"] == "req-1"
        assert result["resume_id"]
        assert result["preview_id"] == registered["preview_id"]
        # Committed retries bypass generation.
        replay = await mdb.claim_preview(
            preview_id=registered["preview_id"],
            source_id=resume["resume_id"],
            job_id=job["job_id"],
            payload_hash="payload-1",
            lease_seconds=60,
        )
        assert replay.token is None
        assert replay.response is not None
        assert replay.response["request_id"] == "req-1"
        # The relation row exists.
        improvement = await mdb.get_improvement_by_tailored_resume(result["resume_id"])
        assert improvement is not None
        assert improvement["request_id"] == "req-1"

    async def test_release_then_reclaim(self, mdb, stored_job_and_resume):
        resume, job = await stored_job_and_resume()
        registered = await self._register(mdb, resume, job)
        claim = await mdb.claim_preview(
            preview_id=registered["preview_id"],
            source_id=resume["resume_id"],
            job_id=job["job_id"],
            payload_hash="payload-1",
            lease_seconds=60,
        )
        await mdb.release_preview_claim(claim)
        reclaim = await mdb.claim_preview(
            preview_id=registered["preview_id"],
            source_id=resume["resume_id"],
            job_id=job["job_id"],
            payload_hash="payload-1",
            lease_seconds=60,
        )
        assert reclaim.token
        assert reclaim.token != claim.token

    async def test_payload_mismatch_rejected(self, mdb, stored_job_and_resume):
        resume, job = await stored_job_and_resume()
        registered = await self._register(mdb, resume, job)
        with pytest.raises(PreviewValidationError):
            await mdb.claim_preview(
                preview_id=registered["preview_id"],
                source_id=resume["resume_id"],
                job_id=job["job_id"],
                payload_hash="other",
                lease_seconds=60,
            )

    async def test_expired_preview_conflicts(self, mdb, stored_job_and_resume):
        resume, job = await stored_job_and_resume()
        registered = await self._register(mdb, resume, job)
        # Force expiry via the underlying mock collection.
        await mdb._db["tailoring_previews"].update_one(
            {"_id": registered["preview_id"]},
            {"$set": {"expires_at": "2000-01-01T00:00:00+00:00"}},
        )
        with pytest.raises(PreviewConflictError):
            await mdb.claim_preview(
                preview_id=registered["preview_id"],
                source_id=resume["resume_id"],
                job_id=job["job_id"],
                payload_hash="payload-1",
                lease_seconds=60,
            )

    async def test_changed_source_conflicts(self, mdb, stored_job_and_resume):
        resume, job = await stored_job_and_resume()
        registered = await self._register(mdb, resume, job)
        await mdb.update_resume(resume["resume_id"], {"content": "changed"})
        with pytest.raises(PreviewConflictError):
            await mdb.claim_preview(
                preview_id=registered["preview_id"],
                source_id=resume["resume_id"],
                job_id=job["job_id"],
                payload_hash="payload-1",
                lease_seconds=60,
            )

    async def test_compat_lookup_without_preview_id(
        self, mdb, stored_job_and_resume
    ):
        resume, job = await stored_job_and_resume()
        registered = await self._register(mdb, resume, job)
        claim = await mdb.claim_preview(
            preview_id=registered["preview_id"],
            source_id=resume["resume_id"],
            job_id=job["job_id"],
            payload_hash="payload-1",
            lease_seconds=60,
        )
        await mdb.complete_preview(
            claim=claim,
            resume_fields={"content": "tailored"},
            response_data={"request_id": "req-9"},
            improvements=[],
        )
        replay = await mdb.claim_preview(
            preview_id=None,
            source_id=resume["resume_id"],
            job_id=job["job_id"],
            payload_hash="payload-1",
            lease_seconds=60,
        )
        assert replay.response is not None
        assert replay.response["request_id"] == "req-9"

    async def test_delete_resume_scrubs_and_prunes_previews(
        self, mdb, stored_job_and_resume
    ):
        resume, job = await stored_job_and_resume()
        registered = await self._register(mdb, resume, job)
        claim = await mdb.claim_preview(
            preview_id=registered["preview_id"],
            source_id=resume["resume_id"],
            job_id=job["job_id"],
            payload_hash="payload-1",
            lease_seconds=60,
        )
        result = await mdb.complete_preview(
            claim=claim,
            resume_fields={"content": "tailored"},
            response_data={"request_id": "req-1"},
            improvements=[],
        )
        # Deleting the result scrubs cached personal data from the replay row.
        assert await mdb.delete_resume(result["resume_id"]) is True
        stored = await mdb._db["tailoring_previews"].find_one(
            {"_id": registered["preview_id"]}
        )
        assert stored is not None
        assert stored["response_data"] is None
        # Deleting the source prunes its previews entirely.
        assert await mdb.delete_resume(resume["resume_id"]) is True
        assert (
            await mdb._db["tailoring_previews"].find_one(
                {"_id": registered["preview_id"]}
            )
            is None
        )

    async def test_delete_job_prunes_previews(self, mdb, stored_job_and_resume):
        resume, job = await stored_job_and_resume()
        registered = await self._register(mdb, resume, job)
        assert await mdb.delete_job(job["job_id"]) is True
        assert (
            await mdb._db["tailoring_previews"].find_one(
                {"_id": registered["preview_id"]}
            )
            is None
        )


class TestImprovements:
    async def test_create_tailored_resume(self, mdb):
        resume = await mdb.create_resume(content="orig")
        job = await mdb.create_job(content="job")
        tailored = await mdb.create_tailored_resume(
            request_id="req-1",
            original_resume_id=resume["resume_id"],
            job_id=job["job_id"],
            resume_fields={"content": "tailored", "parent_id": resume["resume_id"]},
            improvements=[{"path": "summary"}],
        )
        assert tailored["parent_id"] == resume["resume_id"]
        stored = await mdb.get_improvement_by_tailored_resume(tailored["resume_id"])
        assert stored is not None
        assert stored["request_id"] == "req-1"
        assert stored["improvements"] == [{"path": "summary"}]

    async def test_create_improvement(self, mdb):
        created = await mdb.create_improvement("orig", "tailored", "job", [{"a": 1}])
        assert created["request_id"]
        stored = await mdb.get_improvement_by_tailored_resume("tailored")
        assert stored is not None
        assert stored["original_resume_id"] == "orig"

    async def test_get_missing_returns_none(self, mdb):
        assert await mdb.get_improvement_by_tailored_resume("missing") is None


class TestApplications:
    async def test_crud_and_ordering(self, mdb):
        resume = await mdb.create_resume(content="r")
        job = await mdb.create_job(content="j")
        first = await mdb.create_application(job["job_id"], resume["resume_id"])
        assert first["position"] == 0
        assert first["applied_at"]
        assert "_id" not in first
        second = await mdb.create_application(
            job["job_id"], resume["resume_id"] + "-other"
        )
        assert second["position"] == 1
        assert (await mdb.get_application(first["application_id"])) is not None
        assert await mdb.get_application("missing") is None
        listed = await mdb.list_applications()
        assert [app["application_id"] for app in listed] == [
            first["application_id"],
            second["application_id"],
        ]

    async def test_dedupe_returns_existing(self, mdb):
        resume = await mdb.create_resume(content="r")
        job = await mdb.create_job(content="j")
        first = await mdb.create_application(job["job_id"], resume["resume_id"])
        again = await mdb.create_application(job["job_id"], resume["resume_id"])
        assert again["application_id"] == first["application_id"]
        assert len(await mdb.list_applications()) == 1

    async def test_move_repositions_and_renumbers(self, mdb):
        resume = await mdb.create_resume(content="r")
        job = await mdb.create_job(content="j")
        cards = [
            await mdb.create_application(job["job_id"], f"{resume['resume_id']}-{i}")
            for i in range(3)
        ]
        moved = await mdb.update_application(
            cards[2]["application_id"], {"status": "interview", "position": 0}
        )
        assert moved is not None
        assert moved["status"] == "interview"
        assert moved["position"] == 0
        remaining = await mdb.list_applications(status="applied")
        assert [app["position"] for app in remaining] == [0, 1]

    async def test_reorder_within_column(self, mdb):
        resume = await mdb.create_resume(content="r")
        job = await mdb.create_job(content="j")
        cards = [
            await mdb.create_application(job["job_id"], f"{resume['resume_id']}-{i}")
            for i in range(3)
        ]
        moved = await mdb.update_application(cards[0]["application_id"], {"position": 2})
        assert moved is not None
        assert moved["position"] == 2
        listed = await mdb.list_applications(status="applied")
        assert [app["application_id"] for app in listed] == [
            cards[1]["application_id"],
            cards[2]["application_id"],
            cards[0]["application_id"],
        ]
        assert [app["position"] for app in listed] == [0, 1, 2]

    async def test_saved_to_applied_stamps_date(self, mdb):
        resume = await mdb.create_resume(content="r")
        job = await mdb.create_job(content="j")
        card = await mdb.create_application(
            job["job_id"], resume["resume_id"], status="saved"
        )
        assert card["applied_at"] is None
        moved = await mdb.update_application(card["application_id"], {"status": "applied"})
        assert moved is not None
        assert moved["applied_at"]

    async def test_update_missing_returns_none(self, mdb):
        assert await mdb.update_application("missing", {"status": "applied"}) is None

    async def test_bulk_update_and_delete(self, mdb):
        resume = await mdb.create_resume(content="r")
        job = await mdb.create_job(content="j")
        cards = [
            await mdb.create_application(job["job_id"], f"{resume['resume_id']}-{i}")
            for i in range(3)
        ]
        ids = [card["application_id"] for card in cards]
        assert await mdb.bulk_update_applications(ids[:2], "interview") == 2
        assert len(await mdb.list_applications(status="interview")) == 2
        assert await mdb.bulk_delete_applications(ids[:2] + ["missing"]) == 2
        assert len(await mdb.list_applications()) == 1

    async def test_delete_renumbers_column(self, mdb):
        resume = await mdb.create_resume(content="r")
        job = await mdb.create_job(content="j")
        cards = [
            await mdb.create_application(job["job_id"], f"{resume['resume_id']}-{i}")
            for i in range(3)
        ]
        assert await mdb.delete_application(cards[0]["application_id"]) is True
        assert await mdb.delete_application(cards[0]["application_id"]) is False
        remaining = await mdb.list_applications(status="applied")
        assert [app["position"] for app in remaining] == [0, 1]

    async def test_create_manual_application(self, mdb):
        resume = await mdb.create_resume(content="r")
        card = await mdb.create_manual_application(
            content="pasted job", resume_id=resume["resume_id"], company="Acme"
        )
        assert card["company"] == "Acme"
        job = await mdb.get_job(card["job_id"])
        assert job is not None
        assert job["content"] == "pasted job"
        assert job["company"] == "Acme"


class TestApiKeyStore:
    def test_set_get_delete(self, mdb):
        assert mdb.get_api_key_ciphertexts() == {}
        mdb.set_api_key_ciphertext("openai", "ct1")
        mdb.set_api_key_ciphertext("openai", "ct2")
        assert mdb.get_api_key_ciphertexts() == {"openai": "ct2"}
        mdb.delete_api_key("openai")
        assert mdb.get_api_key_ciphertexts() == {}

    def test_replace_and_clear(self, mdb):
        mdb.replace_api_keys({"a": "1", "b": "2", "empty": ""})
        assert mdb.get_api_key_ciphertexts() == {"a": "1", "b": "2"}
        mdb.replace_api_keys({"c": "3"})
        assert mdb.get_api_key_ciphertexts() == {"c": "3"}
        mdb.clear_api_keys()
        assert mdb.get_api_key_ciphertexts() == {}


class TestStatsAndReset:
    async def test_stats(self, mdb):
        resume = await mdb.create_resume(content="r")
        job = await mdb.create_job(content="j")
        await mdb.create_improvement(resume["resume_id"], "t", job["job_id"], [])
        stats = await mdb.get_stats()
        assert stats == {
            "total_resumes": 1,
            "total_jobs": 1,
            "total_improvements": 1,
            "has_master_resume": False,
        }
        await mdb.set_master_resume(resume["resume_id"])
        assert (await mdb.get_stats())["has_master_resume"] is True

    async def test_reset_preserves_api_keys(self, mdb):
        resume = await mdb.create_resume(content="r")
        job = await mdb.create_job(content="j")
        await mdb.create_application(job["job_id"], resume["resume_id"])
        mdb.set_api_key_ciphertext("openai", "ct")
        await mdb.reset_database()
        assert await mdb.list_resumes() == []
        assert await mdb.list_applications() == []
        assert (await mdb.get_stats())["total_jobs"] == 0
        assert mdb.get_api_key_ciphertexts() == {"openai": "ct"}


class TestIndexes:
    async def test_index_creation_runs_and_enforces_specs(self, mongo_clients):
        async_client, sync_client = mongo_clients
        database = MongoDatabase(
            async_client=async_client, sync_client=sync_client, create_indexes=True
        )
        try:
            database._ensure_initialized()  # trigger lazy index creation
            store = sync_client["resume_matcher"]
            index_names = {
                name: info
                for name, info in (
                    (index["name"], index) for index in store["resumes"].list_indexes()
                )
            }
            assert "is_master_1" in index_names
            assert index_names["is_master_1"]["unique"] is True
            app_indexes = {
                index["name"]: index
                for index in store["applications"].list_indexes()
            }
            assert "job_id_1_resume_id_1" in app_indexes
            assert app_indexes["job_id_1_resume_id_1"]["unique"] is True
            # The specs actually constrain writes on the indexed store.
            store["resumes"].insert_one({"_id": "m1", "is_master": True})
            with pytest.raises(DuplicateKeyError):
                store["resumes"].insert_one({"_id": "m2", "is_master": True})
            store["resumes"].insert_one({"_id": "n1", "is_master": False})
            store["resumes"].insert_one({"_id": "n2", "is_master": False})
        finally:
            await database.close()
