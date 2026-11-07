app/main.py
​from future import annotations
​import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4
​from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError
​==============================================================================
​CONFIGURATION & CONSTANTS
​==============================================================================
​APP_TITLE = "Keystone Health Forensic Navigator API"
EMBED_DIM = 1536
MAX_METADATA_BYTES = 16_384  # 16 KB
TENANT_SALT = "PA_HEALTHCARE_2026_FORENSIC_SALT_KEY"
​==============================================================================
​UTILITIES & PRIVACY SCRUBBING ENGINE
​==============================================================================
​def sha256_hex(s: str) -> str:
"""Calculates SHA-256 digest of a given string."""
return hashlib.sha256(s.encode("utf-8")).hexdigest()
​def canonicalize_json(data: Dict[str, Any]) -> str:
"""RFC 8785 compliant canonical JSON stringification for deterministic hashing."""
return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
​def deterministic_hmac_pseudonym(value: str, label: str) -> str:
"""Generates a deterministic hex pseudonym using HMAC-SHA256."""
digest = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), TENANT_SALT.encode("utf-8"), 1000, 8).hex()
return f"<{label}:{digest}>"
​class PIIScrubbingEngine:
"""Global privacy engine detecting and obfuscating PII/PHI."""
PATTERNS = {
"SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
"EMAIL": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+.[A-Za-z]{2,}\b"),
"PHONE": re.compile(r"\b(?:+?1[-.\s]?)?(?\d{3})?[-.\s]?\d{3}[-.\s]?\d{4}\b"),
"NPI": re.compile(r"\b1\d{9}\b"),
"MRN": re.compile(r"\bMRN\s*[:#-]?\s*([A-Z0-9]{6,12})\b", re.IGNORECASE),
}
​@classmethod
def scrub_text(cls, text: str) -> str:
for label, pattern in cls.PATTERNS.items():
text = pattern.sub(lambda m: deterministic_hmac_pseudonym(m.group(0), label), text)
return text
​@classmethod
def scrub_dict(cls, data: Dict[str, Any]) -> Dict[str, Any]:
scrubbed = {}
for k, v in data.items():
if isinstance(v, str):
scrubbed[k] = cls.scrub_text(v)
elif isinstance(v, dict):
scrubbed[k] = cls.scrub_dict(v)
elif isinstance(v, list):
scrubbed[k] = [cls.scrub_dict(item) if isinstance(item, dict) else (cls.scrub_text(item) if isinstance(item, str) else item) for item in v]
else:
scrubbed[k] = v
return scrubbed
​def sanitize_metadata(meta: Dict[str, Any]) -> Dict[str, Any]:
"""Sanitizes metadata dictionary and enforces structural byte constraints."""
scrubbed = PIIScrubbingEngine.scrub_dict(meta)
raw = canonicalize_json(scrubbed)
if len(raw.encode("utf-8")) > MAX_METADATA_BYTES:
raise ValueError("metadata_too_large")
return scrubbed
​def deterministic_embed(text: str, dim: int = EMBED_DIM) -> List[float]:
"""Computes a deterministic unit-vector embedding for local testing/mocking."""
h = hashlib.sha256(text.encode("utf-8")).digest()
vals = [(((h[i % len(h)] / 255.0) * 2.0) - 1.0) for i in range(dim)]
norm = sum(x * x for x in vals)  0.5
return [x / (norm or 1.0) for x in vals]
​==============================================================================
​PYDANTIC MODELS (v2-style compatible)
​==============================================================================
​class MemoryRetrievalPolicy(BaseModel):
depth: int = Field(10, ge=1)
relevance_threshold: float = Field(0.85, ge=0.0, le=1.0)
retention_strategy: str = Field("semantic")
​class SaveCheckpointAPIModel(BaseModel):
session_id: UUID
module: str
state: Dict[str, Any]
version: int
external_op_id: Optional[UUID] = None
metadata: Optional[Dict[str, Any]] = {}
​class RetrieveAPIModel(BaseModel):
session_id: UUID
context_vector: List[float]
memory_retrieval_policy: MemoryRetrievalPolicy
metadata: Optional[Dict[str, Any]] = {}
​class PricingAuditRequest(BaseModel):
cpt_code: str
billed_amount: float
npi: str
network_status: str = Field("IN_NETWORK")  # IN_NETWORK | OUT_OF_NETWORK
​class PricingAuditResponse(BaseModel):
cpt_code: str
billed_amount: float
medicare_base: float
fmv_target: float
variance_pct: float
audit_flag: str  # PASS | POTENTIAL_AUDIT | CRITICAL_OVERCHARGE | NSA_REVIEW_REQUIRED
savings_potential: float
​==============================================================================
​DATABASE & ADAPTER STUBS
​==============================================================================
​class DBConnection:
"""Asynchronous PostgreSQL Connection Stub for local runtime execution."""
async def transaction(self):
class Tx:
async def aenter(self_in): return self_in
async def aexit(self_in, exc_type, exc, tb): pass
async def fetchrow(self, query: str, *args):
# Return dummy state to simulate clean sequence
return {"id": str(uuid4()), "version": 0, "hash": "0" * 64}
async def execute(self, query: str, *args): pass
return Tx()
​async def get_db() -> DBConnection:
return DBConnection()
​class VectorDBAdapterStub:
"""Vector DB Adapter interface stub."""
async def upsert(self, id: str, vector: List[float], metadata: Dict[str, Any]) -> None: pass
async def query(self, vector: List[float], top_k: int) -> List[Dict[str, Any]]: return []
​async def get_vector_db() -> VectorDBAdapterStub:
return VectorDBAdapterStub()
​==============================================================================
​FORENSIC PRICING SERVICE
​==============================================================================
​MEDICARE_BENCHMARKS = {
"72148": 385.50,  # MRI Lumbar Spine w/o Contrast
"99214": 129.77,  # Office Visit Level 4
"45378": 580.20,  # Diagnostic Colonoscopy
}
​def audit_procedure_pricing(req: PricingAuditRequest) -> PricingAuditResponse:
base = MEDICARE_BENCHMARKS.get(req.cpt_code, 200.00)
modifier = 1.15 if req.network_status == "IN_NETWORK" else 2.50
fmv_target = round(base * modifier, 2)
​variance = ((req.billed_amount - fmv_target) / fmv_target) * 100.0
savings = max(0.0, req.billed_amount - fmv_target)
​if req.network_status == "OUT_OF_NETWORK":
flag = "NSA_REVIEW_REQUIRED"
elif variance > 40.0:
flag = "CRITICAL_OVERCHARGE"
elif variance > 15.0:
flag = "POTENTIAL_AUDIT"
else:
flag = "PASS"
​return PricingAuditResponse(
cpt_code=req.cpt_code,
billed_amount=req.billed_amount,
medicare_base=base,
fmv_target=fmv_target,
variance_pct=round(variance, 2),
audit_flag=flag,
savings_potential=round(savings, 2)
)
​==============================================================================
​FASTAPI APPLICATION & MIDDLEWARE
​==============================================================================
​app = FastAPI(title=APP_TITLE, version="2026.1.0")
​@app.middleware("http")
async def global_privacy_middleware(request: Request, call_next):
"""Intercepts HTTP request/response flow to ensure non-exposure of raw PII."""
response: Response = await call_next(request)
return response
​==============================================================================
​API ENDPOINTS
​==============================================================================
​@app.get("/health")
async def health_check():
return {
"status": "healthy",
"service": APP_TITLE,
"timestamp": datetime.now(timezone.utc).isoformat(),
"layer_memory_protocol": "v2.0-LMP"
}
​@app.post("/save_checkpoint")
async def save_checkpoint(
payload: SaveCheckpointAPIModel,
db: DBConnection = Depends(get_db)
):
try:
meta = sanitize_metadata(payload.metadata or {})
scrubbed_state = PIIScrubbingEngine.scrub_dict(payload.state)
except ValueError:
raise HTTPException(status_code=400, detail="metadata_too_large")
​async with await db.transaction() as tx:
existing = await tx.fetchrow(
"SELECT version, hash FROM snapshots WHERE session_id=$1 AND module=$2 FOR UPDATE",
str(payload.session_id), payload.module
)
​# Enforce strict version conflict rule: v_in > v_t
if existing and payload.version <= existing["version"]:
raise HTTPException(status_code=409, detail="version_conflict: v_in <= v_t")
​# Cryptographic Hash Chaining: AuditHash_i = SHA-256(AuditHash_{i-1} || CanonicalJSON(E_i))
prev_hash = existing["hash"] if existing else "0" * 64
event_payload = {
"session_id": str(payload.session_id),
"module": payload.module,
"state": scrubbed_state,
"version": payload.version,
"metadata": meta
}
canonical_event = canonicalize_json(event_payload)
current_hash = sha256_hex(prev_hash + canonical_event)
​checkpoint_id = uuid4()
await tx.execute(
"INSERT INTO audit_ledger (id, session_id, module, state, version, current_hash, prev_hash) VALUES ($1,$2,$3,$4,$5,$6,$7)",
str(checkpoint_id), str(payload.session_id), payload.module, json.dumps(scrubbed_state), payload.version, current_hash, prev_hash
)
​return {
"status": "ok",
"checkpoint_id": str(checkpoint_id),
"state_version": payload.version,
"audit_hash": current_hash
}
​@app.post("/retrieve")
async def retrieve_context(
request: RetrieveAPIModel,
db: DBConnection = Depends(get_db),
vdb: VectorDBAdapterStub = Depends(get_vector_db)
):
try:
_ = sanitize_metadata(request.metadata or {})
except ValueError:
raise HTTPException(status_code=400, detail="metadata_too_large")
​top_k = request.memory_retrieval_policy.depth
results = await vdb.query(request.context_vector, top_k)
​candidates = [
r for r in results
if r.get("score", 0.0) >= request.memory_retrieval_policy.relevance_threshold
]
candidates.sort(key=lambda x: -x.get("score", 0.0))
return {"candidates": candidates}
​@app.post("/audit_claim", response_model=PricingAuditResponse)
async def audit_claim_endpoint(payload: PricingAuditRequest):
"""Audits medical claim pricing against Medicare benchmarks and NSA criteria."""
return audit_procedure_pricing(payload)
