"""Runtime configuration.

Every value here that changes system *behaviour* (model, prompt version, publish
scopes, retention) is read from the environment and therefore from the deployment
manifest - which lives in Git and is changed by Pull Request only (CC8.1).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent
# Local-development HS256 secret (>= 32 bytes per RFC 7518). Prod refuses this value or any short one.
DEV_JWT_SECRET = "dev-only-change-me-before-any-deployment-0123456789"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="AGENT_", extra="ignore", populate_by_name=True)

    env: str = "dev"

    # --- LLM (CC9.2 vendor / confidentiality) -------------------------------
    # Provider: "anthropic" (ZDR-capable; production) or "gemini" (Google AI Studio free
    # tier; learning/non-client use ONLY - free-tier terms permit Google to use prompts,
    # so it fails control C-03 and is refused in prod). Recorded in the CONFIG audit record.
    provider: str = "anthropic"
    gemini_model: str = "gemini-3.8-flash"
    # Ordered fallbacks tried only after the primary model fails with transient errors. The served
    # model is always recorded (lineage) and the artefact is flagged MODEL_FALLBACK for the reviewer.
    gemini_fallback_models: list[str] = ["gemini-3.5-flash", "gemini-3.1-flash-lite"]
    # Read from GEMINI_API_KEY (environment or .env). Never printed: repr=False, excluded from audit.
    gemini_api_key: str = Field(default="", validation_alias="GEMINI_API_KEY", repr=False)
    # Pinned model ID. Changing it is a change-management event (see
    # compliance/MODEL_REGISTRY.md). Never use an alias such as "latest".
    model_id: str = "claude-opus-5-5"
    model_effort: str = "high"
    max_output_tokens: int = 16000
    # Server-side refusal fallback. If enabled, the *served* model may differ from
    # model_id; the audit log records response.model so lineage stays exact.
    enable_refusal_fallback: bool = True
    # Must be true in prod: Anthropic org configured for Zero Data Retention.
    # Startup refuses to run in prod without this acknowledgement.
    zdr_contract_confirmed: bool = False

    # --- Prompts as code (CC8.1) ---------------------------------------------
    prompts_dir: Path = REPO_ROOT / "prompts"
    prompt_version: str = "1.2.0"

    # --- Brand analysis crawl (CC6.6 / CC6.7 boundary controls) -----------------
    # Per-tenant crawl scope lives in tenants/brands.yaml (change-managed).
    brands_file: Path = REPO_ROOT / "tenants" / "brands.yaml"
    crawl_max_bytes_per_page: int = 2_000_000
    brand_context_max_chars: int = 60_000

    # --- Data store -------------------------------------------------------------
    database_url: str = "sqlite:///./agent.db"

    # --- RAG (CC6.1 / CC6.3 least privilege, C1.1 tenant isolation) -----------
    # Must point at a role with SELECT only. Verified at startup.
    rag_database_url: str | None = None
    rag_top_k: int = 6

    # --- Audit / WORM (CC7.2, CC7.3) ------------------------------------------
    audit_s3_bucket: str | None = None  # bucket MUST have Object Lock (COMPLIANCE mode)
    audit_s3_prefix: str = "content-agent/audit/"
    audit_local_dir: Path = REPO_ROOT / "audit_log"
    audit_retention_days: int = 365 * 7

    # --- AuthN / AuthZ (CC6.1, CC6.2) ------------------------------------------
    # HS256 for local dev; production uses the OIDC provider's JWKS (RS256).
    jwt_secret: str = Field(default=DEV_JWT_SECRET, repr=False)
    jwt_issuer: str = "https://idp.example.com/"
    jwt_audience: str = "content-agent"

    # --- Publishing (PI1.4 / PI1.5 output completeness & accuracy) -------------
    # WordPress application passwords are per tenant and are NOT Settings fields:
    # they are read from AGENT_WP_USER__<TENANT> / AGENT_WP_APP_PASSWORD__<TENANT>
    # only inside the reviewer-triggered publish path (see cms.load_wordpress_credentials).

    # --- Privacy (P4.2 retention / P4.3 disposal) -------------------------------
    draft_retention_days: int = 180

    @property
    def effective_model_id(self) -> str:
        return self.gemini_model if self.provider == "gemini" else self.model_id

    @property
    def data_retention_class(self) -> str:
        """What the vendor does with our prompts - stamped into every CONFIG audit record."""
        if self.provider == "gemini":
            return "FREE_TIER_NOT_ZDR_MAY_BE_USED_FOR_TRAINING"
        return "ZDR_CONTRACT" if self.zdr_contract_confirmed else "STANDARD_RETENTION_NOT_ZDR"

    def validate_for_environment(self) -> None:
        if self.provider not in {"anthropic", "gemini"}:
            raise RuntimeError(f"AGENT_PROVIDER must be anthropic or gemini, got {self.provider!r}")
        if self.env == "prod":
            problems = []
            if self.provider != "anthropic":
                problems.append("free-tier providers are not permitted in prod (control C-03: Zero Data Retention)")
            if not self.zdr_contract_confirmed:
                problems.append("AGENT_ZDR_CONTRACT_CONFIRMED must be true in prod")
            if not self.audit_s3_bucket:
                problems.append("AGENT_AUDIT_S3_BUCKET (Object Lock) is required in prod")
            if self.jwt_secret == DEV_JWT_SECRET or len(self.jwt_secret.encode()) < 32:
                problems.append("AGENT_JWT_SECRET is the dev default")
            if self.database_url.startswith("sqlite"):
                problems.append("SQLite is not permitted in prod")
            if problems:
                raise RuntimeError("Refusing to start: " + "; ".join(problems))


@lru_cache
def get_settings() -> Settings:
    # Per-tenant publish credentials (AGENT_WP_*) are deliberately NOT Settings fields; they are
    # read from the process environment only on the publish path. Load .env into the process
    # environment here so local runs behave like a deployment where the secrets manager injects them.
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env", override=False)
    s = Settings()
    s.validate_for_environment()
    return s
