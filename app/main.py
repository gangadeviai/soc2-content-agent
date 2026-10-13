"""FastAPI entry point - the security proxy gateway in front of the model.

Pipeline:  POST /brands/analyze -> POST /brands/{id}/review (confirm)
        -> POST /strategies     -> POST /strategies/{id}/review (approve)
        -> POST /strategies/{id}/items/{n}/draft  (or POST /drafts with a free brief)
        -> POST /review/{draft_id} (approve) -> POST /review/{draft_id}/publish
"""

import json
import os
import time
import uuid
from contextlib import asynccontextmanager

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import brand as brand_stage
from . import strategy as strategy_stage
from . import workflow
from .audit import AuditRecord, AuditSink
from .auth import ROLE_ADMIN, ROLE_REQUESTER, ROLE_REVIEWER, Principal, mint_dev_token, require_role
from .brand import BrandConfig, FakeFetcher, HttpFetcher
from .cms import FakePublisher, WordPressPublisher, load_wordpress_credentials
from .config import REPO_ROOT, Settings, get_settings
from .demo_data import DEMO_SITE
from .llm import FakeLLMClient, make_llm_client
from .models import BrandProfile, ContentStrategy, Draft, DraftStatus, StrategyStatus, make_session_factory
from .prompts import PromptRegistry
from .rag import Chunk, InMemoryRetriever, PgVectorRetriever
from .seo import SCORE_THRESHOLD


# ---- request schemas -----------------------------------------------------------
class AnalyzeRequest(BaseModel):
    notes: str = Field(default="", max_length=4000, description="Agency notes to steer the analysis (optional).")


class StrategyRequest(BaseModel):
    objectives: str = Field(min_length=10, max_length=4000)
    horizon_weeks: int = Field(default=8, ge=1, le=26)


class GenerateRequest(BaseModel):
    brief: str = Field(min_length=20, max_length=20000)
    content_type: str = Field(
        default="blog_post", pattern=r"^(blog_post|landing_page|email|social_post|press_release)$"
    )


class ReviewRequest(BaseModel):
    decision: str
    note: str = ""


class DeleteRequest(BaseModel):
    reason: str = Field(min_length=5)


OFFLINE_ENVS = {"test", "demo"}
STATIC_DIR = REPO_ROOT / "app" / "static"


def load_brands(path) -> dict[str, BrandConfig]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {tenant: BrandConfig.from_yaml(tenant, cfg) for tenant, cfg in raw.items()}


def build_services(
    settings: Settings, *, llm=None, retriever=None, publisher_factory=None, fetcher=None
) -> workflow.Services:
    prompts = PromptRegistry(settings.prompts_dir).load(settings.prompt_version)  # fails closed on hash mismatch
    audit = AuditSink(
        settings.audit_local_dir, settings.audit_s3_bucket, settings.audit_s3_prefix, settings.audit_retention_days
    )
    audit.write(
        AuditRecord(
            "CONFIG",
            "system",
            "*",
            None,
            {
                "event": "startup",
                **prompts.lineage,
                "provider": settings.provider,
                "model_id": settings.effective_model_id,
                "data_retention_class": settings.data_retention_class,  # the vendor-risk fact, on the record
                "env": settings.env,
                "zdr_contract_confirmed": settings.zdr_contract_confirmed,
            },
        )
    )
    brands = load_brands(settings.brands_file)

    if retriever is None:
        if settings.rag_database_url:
            retriever = PgVectorRetriever(settings.rag_database_url, embed=_embed_stub)
        else:
            retriever = InMemoryRetriever(_demo_chunks())
    offline = settings.env in OFFLINE_ENVS  # test + demo: no network, no secrets, deterministic fakes
    if llm is None:
        llm = FakeLLMClient() if offline else make_llm_client(settings)
    if fetcher is None:
        if offline:
            fetcher = FakeFetcher(DEMO_SITE if settings.env == "demo" else {})
        else:
            fetcher = HttpFetcher(max_bytes=settings.crawl_max_bytes_per_page)
    if publisher_factory is None:
        if offline:
            fake = FakePublisher()

            def publisher_factory(tenant_id: str):
                return fake
        else:

            def publisher_factory(tenant_id: str):
                cfg = brands.get(tenant_id)
                if cfg is None:
                    raise workflow.PublishCredentialsError(f"no brand website configured for tenant {tenant_id!r}")
                user, password = load_wordpress_credentials(tenant_id)  # read only on the publish path
                return WordPressPublisher(
                    cfg.wordpress_url,
                    user,
                    password,
                    allowed_host=cfg.effective_publish_domain,
                    seo_plugin=cfg.seo_plugin,
                    allow_local=settings.env != "prod",  # local Docker WordPress for learning; never in prod
                    platform=cfg.wordpress_platform,
                )

    deny_path = REPO_ROOT / "tenants" / "deny_terms.yaml"
    deny = yaml.safe_load(deny_path.read_text(encoding="utf-8")) if deny_path.exists() else {}
    return workflow.Services(
        prompts=prompts,
        llm=llm,
        retriever=retriever,
        audit=audit,
        publisher_factory=publisher_factory,
        fetcher=fetcher,
        brands=brands,
        tenant_deny_terms=deny or {},
        rag_top_k=settings.rag_top_k,
        brand_context_max_chars=settings.brand_context_max_chars,
    )


def _embed_stub(text: str) -> list[float]:  # replace with your embedding provider (also under ZDR)
    raise NotImplementedError("configure an embedding function for PgVectorRetriever")


def _demo_chunks() -> list[Chunk]:
    return [
        Chunk(
            "a-1",
            "client-a",
            "brand_voice.md",
            "Client A brand voice: confident, plain English, no jargon. Audience: engineering leaders.",
        ),
        Chunk(
            "a-2",
            "client-a",
            "product.md",
            "Client A platform reduces deployment time with automated rollbacks and preview environments.",
        ),
        Chunk(
            "b-1",
            "client-b",
            "brand_voice.md",
            "Client B voice: playful, consumer-focused. Product: subscription meal kits.",
        ),
    ]


def create_app(settings: Settings | None = None, **overrides) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.services = build_services(settings, **overrides)
        app.state.audit = app.state.services.audit
        app.state.sessions = make_session_factory(settings.database_url)
        yield

    app = FastAPI(title="SOC 2-ready Marketing Content Agent", version="1.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        response.headers["x-process-time-ms"] = f"{(time.perf_counter() - start) * 1000:.1f}"
        return response

    def db(request: Request) -> Session:
        session = request.app.state.sessions()
        try:
            yield session
        finally:
            session.close()

    def svc(request: Request) -> workflow.Services:
        return request.app.state.services

    requester = Depends(require_role(ROLE_REQUESTER))
    reviewer = Depends(require_role(ROLE_REVIEWER))
    admin = Depends(require_role(ROLE_ADMIN))

    # ---- health -------------------------------------------------------------------
    @app.get("/healthz")
    def healthz(request: Request):
        ok, n = request.app.state.audit.verify_chain()
        return {
            "status": "ok" if ok else "AUDIT_CHAIN_BROKEN",
            "audit_records_verified": n,
            "prompt_version": request.app.state.services.prompts.version,
            "provider": settings.provider,
            "model": settings.effective_model_id,
            "data_retention_class": settings.data_retention_class,
            "env": settings.env,
        }

    # ---- stage 1: brand analysis ----------------------------------------------------
    @app.post("/brands/analyze", status_code=201)
    def analyze_brand(
        body: AnalyzeRequest,
        principal: Principal = requester,
        session: Session = Depends(db),
        services: workflow.Services = Depends(svc),
    ):
        return brand_stage.analyze_brand(session, services, principal, body.notes).to_public()

    @app.get("/brands")
    def list_brands(principal: Principal = requester, session: Session = Depends(db)):
        q = session.query(BrandProfile).filter(BrandProfile.tenant_id == principal.tenant_id)
        return [b.to_public() for b in q.order_by(BrandProfile.version.desc()).limit(50)]

    @app.get("/brands/{brand_id}")
    def get_brand(brand_id: str, principal: Principal = requester, session: Session = Depends(db)):
        return workflow.load_owned(session, BrandProfile, brand_id, principal).to_public()

    @app.post("/brands/{brand_id}/review")
    def review_brand(
        brand_id: str,
        body: ReviewRequest,
        principal: Principal = reviewer,
        session: Session = Depends(db),
        services: workflow.Services = Depends(svc),
    ):
        return brand_stage.review_brand(session, services, principal, brand_id, body.decision, body.note).to_public()

    # ---- stage 2: strategy -----------------------------------------------------------
    @app.post("/strategies", status_code=201)
    def create_strategy(
        body: StrategyRequest,
        principal: Principal = requester,
        session: Session = Depends(db),
        services: workflow.Services = Depends(svc),
    ):
        return strategy_stage.plan_strategy(
            session, services, principal, body.objectives, body.horizon_weeks
        ).to_public()

    @app.get("/strategies")
    def list_strategies(principal: Principal = requester, session: Session = Depends(db)):
        q = session.query(ContentStrategy).filter(ContentStrategy.tenant_id == principal.tenant_id)
        return [s.to_public() for s in q.order_by(ContentStrategy.created_at.desc()).limit(50)]

    @app.get("/strategies/{strategy_id}")
    def get_strategy(strategy_id: str, principal: Principal = requester, session: Session = Depends(db)):
        return workflow.load_owned(session, ContentStrategy, strategy_id, principal).to_public()

    @app.post("/strategies/{strategy_id}/review")
    def review_strategy(
        strategy_id: str,
        body: ReviewRequest,
        principal: Principal = reviewer,
        session: Session = Depends(db),
        services: workflow.Services = Depends(svc),
    ):
        return strategy_stage.review_strategy(
            session, services, principal, strategy_id, body.decision, body.note
        ).to_public()

    @app.post("/strategies/{strategy_id}/items/{item_index}/draft", status_code=201)
    def draft_from_strategy_item(
        strategy_id: str,
        item_index: int,
        principal: Principal = requester,
        session: Session = Depends(db),
        services: workflow.Services = Depends(svc),
    ):
        return strategy_stage.draft_from_item(session, services, principal, strategy_id, item_index).to_public()

    # ---- stage 3: writing ------------------------------------------------------------
    @app.post("/drafts", status_code=201)
    def create_draft(
        body: GenerateRequest,
        principal: Principal = requester,
        session: Session = Depends(db),
        services: workflow.Services = Depends(svc),
    ):
        return workflow.generate(session, services, principal, body.brief, body.content_type).to_public()

    @app.get("/drafts")
    def list_drafts(status: str | None = None, principal: Principal = requester, session: Session = Depends(db)):
        q = session.query(Draft).filter(Draft.tenant_id == principal.tenant_id)  # always tenant-scoped
        if status:
            q = q.filter(Draft.status == DraftStatus(status))
        return [d.to_public() for d in q.order_by(Draft.created_at.desc()).limit(200)]

    @app.get("/drafts/{draft_id}")
    def get_draft(draft_id: str, principal: Principal = requester, session: Session = Depends(db)):
        return workflow._load_for_tenant(session, draft_id, principal).to_public()

    # ---- stage 4: review & publish -----------------------------------------------------
    @app.post("/review/{draft_id}")
    def review_draft(
        draft_id: str,
        body: ReviewRequest,
        principal: Principal = reviewer,
        session: Session = Depends(db),
        services: workflow.Services = Depends(svc),
    ):
        return workflow.review(session, services, principal, draft_id, body.decision, body.note).to_public()

    @app.post("/review/{draft_id}/publish")
    def publish_draft(
        draft_id: str,
        principal: Principal = reviewer,
        session: Session = Depends(db),
        services: workflow.Services = Depends(svc),
    ):
        return workflow.publish(session, services, principal, draft_id).to_public()

    @app.delete("/drafts/{draft_id}", status_code=204)
    def delete_draft(
        draft_id: str,
        body: DeleteRequest,
        principal: Principal = admin,
        session: Session = Depends(db),
        services: workflow.Services = Depends(svc),
    ):
        workflow.delete_draft(session, services, principal, draft_id, body.reason)

    # ---- admin / monitoring -------------------------------------------------------------
    @app.get("/admin/audit/verify")
    def verify_audit(request: Request, principal: Principal = admin):
        ok, n = request.app.state.audit.verify_chain()
        return {"chain_intact": ok, "records": n}

    @app.get("/admin/audit/records")
    def audit_records(request: Request, object_ids: str = "", limit: int = 200, principal: Principal = admin):
        """Lineage pack: every audit record for the given brand / strategy / draft ids (comma-separated),
        or the most recent records when none are given. Admin-only, tenant-scoped."""
        wanted = {i for i in object_ids.split(",") if i}
        path = request.app.state.audit.path
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        rows = [r for r in rows if r["tenant_id"] in {principal.tenant_id, "*"}]
        if wanted:
            rows = [r for r in rows if r.get("draft_id") in wanted]
        return rows[-limit:]

    @app.get("/admin/metrics")
    def metrics(principal: Principal = admin, session: Session = Depends(db)):
        """Output-quality monitoring feed (CC7.1 / CC4.1) across all pipeline stages."""
        drafts = session.query(Draft).all()
        strategies = session.query(ContentStrategy).all()
        brands = session.query(BrandProfile).all()
        total = len(drafts) or 1
        return {
            "brands_total": len(brands),
            "brands_pending_confirmation": sum(1 for b in brands if b.status.value == "pending_confirmation"),
            "brands_with_flags": sum(1 for b in brands if b.review_flags),
            "strategies_total": len(strategies),
            "strategies_approved": sum(1 for s in strategies if s.status == StrategyStatus.APPROVED),
            "strategies_with_flags": sum(1 for s in strategies if s.review_flags),
            "drafts_total": len(drafts),
            "refused": sum(1 for d in drafts if d.status == DraftStatus.REFUSED),
            "with_review_flags": sum(1 for d in drafts if d.review_flags),
            "flag_rate": round(sum(1 for d in drafts if d.review_flags) / total, 3),
            "approved_or_published": sum(
                1 for d in drafts if d.status in {DraftStatus.APPROVED, DraftStatus.PUBLISHED}
            ),
            "published_unverified": sum(1 for d in drafts if d.publish_verified is False),
            "rejected": sum(1 for d in drafts if d.status == DraftStatus.REJECTED),
            "seo_score_avg": round(
                sum(d.seo_score for d in drafts if d.seo_score is not None)
                / max(1, sum(1 for d in drafts if d.seo_score is not None)),
                1,
            ),
            "seo_below_threshold": sum(1 for d in drafts if d.seo_score is not None and d.seo_score < SCORE_THRESHOLD),
            "model_fallbacks": sum(1 for d in drafts if any(f.startswith("MODEL_FALLBACK") for f in d.review_flags)),
        }

    if settings.env in {"dev", "test", "demo"}:

        @app.post("/dev/token")
        def dev_token(sub: str, tenant_id: str, roles: str = "requester"):
            return {"token": mint_dev_token(sub, tenant_id, roles.split(","), settings)}

        @app.get("/demo", include_in_schema=False)
        def demo_ui():
            return FileResponse(STATIC_DIR / "demo.html")

        class OAuthCapture(BaseModel):
            tenant_id: str = Field(pattern=r"^[a-z0-9-]+$")
            access_token: str = Field(min_length=10, max_length=4096)

        @app.post("/dev/oauth/capture", include_in_schema=False)
        def capture_oauth_token(body: OAuthCapture, request: Request):
            """Local-dev convenience for the WordPress.com implicit OAuth flow: the browser posts the
            token from the redirect fragment; it is stored in .env (git-ignored) and the process env.
            Never enabled in prod; the token itself is never logged or audited."""
            if body.tenant_id not in request.app.state.services.brands:
                raise HTTPException(404, "unknown tenant")
            key = f"AGENT_WP_APP_PASSWORD__{body.tenant_id.upper().replace('-', '_')}"
            env_path = REPO_ROOT / ".env"
            lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
            lines = [ln for ln in lines if not ln.startswith(key + "=")] + [f"{key}={body.access_token}"]
            env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.environ[key] = body.access_token  # effective immediately, no restart
            request.app.state.audit.write(
                AuditRecord(
                    "CONFIG",
                    "dev-ui",
                    body.tenant_id,
                    None,
                    {"event": "wordpress_publish_credential_stored", "env_key": key, "source": "oauth_implicit"},
                )
            )
            return {"stored": key, "tenant_id": body.tenant_id}

        @app.get("/", include_in_schema=False)
        def root():
            return RedirectResponse("/demo")

    return app


app = create_app() if __name__ == "app.main" else None
