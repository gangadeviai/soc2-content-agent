"""Immutable, hash-chained audit trail (CC7.2 monitoring, CC7.3 evaluation, CC4.1).

Every record carries ``prev_hash`` and ``record_hash`` so tampering with or
deleting any record breaks the chain. Records are shipped to an S3 bucket with
Object Lock in COMPLIANCE mode (nobody - including root - can delete before the
retention date). A local JSONL mirror is kept for dev and for forensic
reconstruction.

What we log for every generation (the auditor's "entire chain"):
  requester identity, tenant, prompt version + hash, model requested + model
  served, redaction summary (counts only, never originals), retrieved chunk IDs
  and hashes, SHA-256 of the redacted prompt and of the raw output, token usage,
  stop_reason, injection/claim flags, provider request-id.
We do NOT log raw PII - the redacted prompt is hashed, not stored.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class AuditRecord:
    event_type: str  # GENERATION | REVIEW | PUBLISH | REJECT | ACCESS_DENIED | CONFIG | DELETE
    actor_id: str
    tenant_id: str
    draft_id: str | None
    details: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    prev_hash: str = ""
    record_hash: str = ""

    def seal(self, prev_hash: str) -> AuditRecord:
        self.prev_hash = prev_hash
        body = asdict(self)
        body.pop("record_hash")
        self.record_hash = sha256_text(_canonical(body))
        return self


class AuditSink:
    """Append-only sink: local JSONL mirror + optional S3 Object Lock bucket."""

    def __init__(self, local_dir: Path, s3_bucket: str | None = None, s3_prefix: str = "", retention_days: int = 2555):
        self.local_dir = local_dir
        self.local_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.local_dir / "audit.jsonl"
        self.s3_bucket = s3_bucket
        self.s3_prefix = s3_prefix
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._s3 = None
        if s3_bucket:
            import boto3  # lazy import; not needed in dev

            self._s3 = boto3.client("s3")
            self._assert_object_lock()
        self._last_hash = self._load_last_hash()

    # ---- integrity -----------------------------------------------------------
    def _assert_object_lock(self) -> None:
        cfg = self._s3.get_object_lock_configuration(Bucket=self.s3_bucket)
        rule = cfg.get("ObjectLockConfiguration", {}).get("Rule", {}).get("DefaultRetention", {})
        if rule.get("Mode") != "COMPLIANCE":
            raise RuntimeError(f"Audit bucket {self.s3_bucket} must have Object Lock in COMPLIANCE mode")

    def _load_last_hash(self) -> str:
        if not self.path.exists():
            return "GENESIS"
        last = ""
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
        return json.loads(last)["record_hash"] if last else "GENESIS"

    def read_all(self) -> list[dict]:
        """Read-only view of the local mirror (oldest first). Used by governance views;
        never used to write or alter records."""
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def verify_chain(self) -> tuple[bool, int]:
        """Walk the local mirror and recompute every hash. Returns (ok, records_checked)."""
        prev = "GENESIS"
        n = 0
        if not self.path.exists():
            return True, 0
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = json.loads(line)
                stored = rec.pop("record_hash")
                if rec["prev_hash"] != prev or sha256_text(_canonical(rec)) != stored:
                    return False, n
                prev = stored
                n += 1
        return True, n

    # ---- write --------------------------------------------------------------
    def write(self, record: AuditRecord) -> AuditRecord:
        with self._lock:
            record.seal(self._last_hash)
            line = _canonical(asdict(record))
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if self._s3:
                key = f"{self.s3_prefix}{record.ts[:10]}/{record.record_hash}.json"
                self._s3.put_object(
                    Bucket=self.s3_bucket,
                    Key=key,
                    Body=line.encode("utf-8"),
                    ContentType="application/json",
                    ObjectLockMode="COMPLIANCE",
                    ObjectLockRetainUntilDate=datetime.now(UTC) + timedelta(days=self.retention_days),
                )
            self._last_hash = record.record_hash
            return record
