import asyncio
import json
import httpx
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.api.admin.session import get_session, login_redirect
from app.api.admin._templates import templates
from app.config import settings
from app.db.providers import (
    get_all_provider_settings,
    get_provider_key_models_db,
    update_provider_models,
    cache_provider_models,
    get_vad_engine,
    set_vad_engine,
    toggle_provider_active,
)
from app.db.provider_keys import (
    KEY_ID_OFFSET,
    get_active_keys,
    get_all_keys_for_admin,
    list_db_keys,
    add_key as db_add_key,
    set_key_active as db_set_key_active,
    set_legacy_key_active as db_set_legacy_key_active,
    set_key_model as db_set_key_model,
    delete_key as db_delete_key,
)
from app.core.secrets_crypto import mask_key
from app.db.provider_stats import get_summary, get_by_key, PRICE_PER_MIN
from app.config import settings as _settings

_KEYED_PROVIDERS = {"groq", "deepgram", "fireworks", "together", "openai"}

router = APIRouter()

_PROVIDER_META = {
    "groq": {
        "label":       "Groq Whisper",
        "description": "Batch REST · free 20 RPM/key · muy barato",
        "local":       False,
    },
    "deepgram": {
        "label":       "Deepgram Nova",
        "description": "Batch REST · 50 concurrentes/key",
        "local":       False,
    },
    "deepgramv2": {
        "label":       "Deepgram Nova v2 ⚡",
        "description": "Streaming WebSocket · transcript en tiempo real",
        "local":       False,
    },
    "fireworks": {
        "label":       "Fireworks Whisper",
        "description": "Batch REST · sin límite RPM · OpenAI-compatible",
        "local":       False,
    },
    "together": {
        "label":       "Together AI",
        "description": "Batch REST · cobra por segundo real · sin mínimo · Whisper + NVIDIA Parakeet",
        "local":       False,
    },
    "openai": {
        "label":       "OpenAI Transcribe",
        "description": "Batch REST · gpt-4o-mini-transcribe · mejor contexto que Whisper · $0.003/min",
        "local":       False,
    },
    "vosk": {
        "label":       "Vosk Batch (local)",
        "description": "Local CPU · sin red · modelo español · transcripción post-VAD",
        "local":       True,
    },
    "vosk_stream": {
        "label":       "Vosk Streaming (local)",
        "description": "Local CPU · sin red · procesa audio en paralelo con el VAD · sin latencia extra",
        "local":       True,
    },
    "sherpa": {
        "label":       "Sherpa-onnx (local)",
        "description": "Local CPU · ONNX int8 · Whisper turbo · cero costo",
        "local":       True,
    },
    "sherpa_large": {
        "label":       "Sherpa Whisper Large (local)",
        "description": "Local CPU · Whisper large-v3 completo · más preciso, ~seg/clip (no recomendado como fallback automático) · opt-in — ver INSTALL_SHERPA_LARGE en credentials.conf",
        "local":       True,
    },
}


def _all_keys(keys_str: str, single: str) -> list[str]:
    src = keys_str or single or ""
    return [k.strip() for k in src.split(",") if k.strip()]




_mask_key = mask_key  # alias — mismo criterio que las keys guardadas en DB


async def _get_provider_key_entries(provider: str) -> list[dict]:
    """[{id, key}] — env legacy (id 0,1,2...) + DB activas (id KEY_ID_OFFSET+n),
    misma fuente que usa la rotación real en amd_engine.py. Para "has_key" y
    similares — keys realmente usables, no incluye desactivadas."""
    if provider == "deepgramv2":
        provider = "deepgram"
    if provider not in _KEYED_PROVIDERS:
        return []
    return await get_active_keys(provider)


async def _get_provider_key_entries_admin(provider: str) -> list[dict]:
    """[{id, key, source, active}] — TODAS las keys (activas e inactivas) de
    ambas fuentes, para el modal del panel — a diferencia de
    _get_provider_key_entries(), acá se necesitan ver también las
    desactivadas para poder volver a activarlas."""
    if provider == "deepgramv2":
        provider = "deepgram"
    if provider not in _KEYED_PROVIDERS:
        return []
    return await get_all_keys_for_admin(provider)


def _has_vosk() -> bool:
    from app.core.local_asr import vosk_loaded
    return vosk_loaded()

def _has_sherpa() -> bool:
    from app.core.local_asr import sherpa_loaded
    return sherpa_loaded()

def _has_sherpa_large() -> bool:
    from app.core.local_asr import sherpa_large_loaded
    return sherpa_large_loaded()

_HAS_LOCAL = {
    "vosk":          _has_vosk,
    "vosk_stream":   _has_vosk,
    "sherpa":        _has_sherpa,
    "sherpa_large":  _has_sherpa_large,
}


@router.get("/providers", response_class=HTMLResponse)
async def providers_page(request: Request):
    if not get_session(request):
        return login_redirect(request)
    rows = await get_all_provider_settings()
    by_provider = {r["provider"]: r for r in rows}
    providers = []
    for p, meta in _PROVIDER_META.items():
        row = by_provider.get(p, {"model": "—", "updated_at": None, "active": 1})
        if p in _HAS_LOCAL:
            has_key = _HAS_LOCAL[p]()
        else:
            has_key = bool(await _get_provider_key_entries(p))
        providers.append({
            "provider":    p,
            "label":       meta["label"],
            "description": meta["description"],
            "model":       row["model"],
            "has_key":     has_key,
            "is_local":    meta["local"],
            "active":      bool(row.get("active", 1)),
            "updated_at":  row.get("updated_at"),
        })
    from app.core.silero_vad import silero_loaded
    from app.core.local_asr import get_model_versions
    vad_engine = await get_vad_engine()
    versions   = get_model_versions(settings.MODELS_BASE)
    return templates.TemplateResponse(request, "providers.html", {
        "request":        request,
        "providers":      providers,
        "admin_prefix":   settings.ADMIN_PREFIX,
        "vad_engine":     vad_engine,
        "silero_loaded":  silero_loaded(),
        "model_versions": versions,   # {"vosk": "vosk-model-es-0.42", "silero": "v5.1", ...}
    })


@router.get("/providers/keys", response_class=HTMLResponse)
async def providers_keys_page(request: Request):
    """Página propia para gestionar API keys (v1.16.2) — antes vivía apretado
    dentro del modal "Editar" de /admin/providers, compartido con la
    selección de modelo. Una sección por proveedor cloud, con espacio real."""
    if not get_session(request):
        return login_redirect(request)
    providers_info = [
        {"provider": p, "label": meta["label"], "description": meta["description"]}
        for p, meta in _PROVIDER_META.items() if p in _KEYED_PROVIDERS
    ]
    return templates.TemplateResponse(request, "provider_keys.html", {
        "request":        request,
        "admin_prefix":   settings.ADMIN_PREFIX,
        "active_page":    "provider_keys",
        "providers_info": providers_info,
    })


@router.post("/providers/{provider}/toggle")
async def toggle_provider(request: Request, provider: str):
    if not get_session(request):
        return login_redirect(request)
    if provider not in _PROVIDER_META:
        return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/providers", status_code=302)
    await toggle_provider_active(provider)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/providers?saved=1", status_code=302)


@router.post("/providers/vad-engine")
async def update_vad_engine(request: Request, engine: str = Form(...)):
    if not get_session(request):
        return login_redirect(request)
    if engine not in ("stream", "silero"):
        return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/providers", status_code=302)
    await set_vad_engine(engine)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/providers?saved=1", status_code=302)


@router.post("/providers/{provider}/model")
async def update_model(request: Request, provider: str, key_models_json: str = Form(...)):
    """Recibe JSON: {"global": "modelo", "keys": {"0": "modelo-a", "100001": "modelo-b"}}
    ids < KEY_ID_OFFSET son keys legacy de .env (van a provider_settings.key_models,
    mecanismo de siempre); ids >= KEY_ID_OFFSET son de provider_keys (agregadas desde
    el panel, v1.16.0) — su modelo vive en esa tabla, no en el JSON legacy."""
    if not get_session(request):
        return login_redirect(request)
    if provider not in _PROVIDER_META:
        return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/providers", status_code=302)
    try:
        data         = json.loads(key_models_json)
        global_model = data.get("global", "").strip()
        raw_keys     = {str(k): v.strip() for k, v in data.get("keys", {}).items() if v.strip()}
    except Exception:
        return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/providers", status_code=302)

    legacy_models: dict[str, str] = {}
    for k, model in raw_keys.items():
        kid = int(k)
        if kid >= KEY_ID_OFFSET:
            await db_set_key_model(provider, kid - KEY_ID_OFFSET, model)
        else:
            legacy_models[k] = model

    await update_provider_models(provider, global_model, legacy_models)
    await cache_provider_models(provider, global_model, legacy_models)
    return RedirectResponse(url=f"{settings.ADMIN_PREFIX}/providers?saved=1", status_code=302)


@router.post("/providers/{provider}/keys/add")
async def add_provider_key(request: Request, provider: str, key: str = Form(...), model: str = Form("")):
    """Guarda una key nueva cifrada en DB (v1.16.0) — el valor en texto plano
    nunca se vuelve a devolver después de esta respuesta."""
    if not get_session(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if provider not in _KEYED_PROVIDERS:
        return JSONResponse({"ok": False, "error": "proveedor inválido"}, status_code=400)
    key = key.strip()
    if not key:
        return JSONResponse({"ok": False, "error": "key vacía"}, status_code=400)
    new_id = await db_add_key(provider, key, model.strip())
    return JSONResponse({"ok": True, "id": KEY_ID_OFFSET + new_id, "masked": mask_key(key)})


@router.post("/providers/{provider}/keys/{key_id}/toggle")
async def toggle_provider_key(request: Request, provider: str, key_id: int, active: str = Form(...)):
    """key_id < KEY_ID_OFFSET → key legacy de .env: no se puede borrar (el valor
    real sigue en credentials.conf), pero sí desactivar para la rotación
    (v1.16.1, provider_legacy_disabled — no reescribe el archivo)."""
    if not get_session(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if key_id < KEY_ID_OFFSET:
        await db_set_legacy_key_active(provider, key_id, active == "1")
    else:
        await db_set_key_active(key_id - KEY_ID_OFFSET, active == "1")
    return JSONResponse({"ok": True})


@router.post("/providers/{provider}/keys/{key_id}/delete")
async def delete_provider_key(request: Request, provider: str, key_id: int):
    if not get_session(request):
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    if key_id < KEY_ID_OFFSET:
        return JSONResponse(
            {"ok": False, "error": "las keys de credentials.conf se borran editando el archivo, no desde acá"},
            status_code=400,
        )
    await db_delete_key(key_id - KEY_ID_OFFSET)
    return JSONResponse({"ok": True})


@router.get("/providers/{provider}/models")
async def list_models(request: Request, provider: str):
    """Devuelve modelos disponibles por key: [{index, masked, models, current_model, source, active}]"""
    if not get_session(request):
        return JSONResponse({"keys": []}, status_code=401)

    # Proveedores locales: no tienen API key, devuelven estado del modelo cargado
    if _PROVIDER_META.get(provider, {}).get("local"):
        from app.core.local_asr import vosk_loaded, sherpa_loaded, sherpa_large_loaded, get_model_versions
        versions = get_model_versions(settings.MODELS_BASE)
        if provider in ("vosk", "vosk_stream"):
            loaded  = vosk_loaded()
            version = versions.get("vosk", "")
        elif provider == "sherpa_large":
            loaded  = sherpa_large_loaded()
            version = versions.get("sherpa_large", "")
        else:
            loaded  = sherpa_loaded()
            version = versions.get("sherpa", "")
        return JSONResponse({
            "local":   True,
            "loaded":  loaded,
            "version": version or "(sin modelo — ejecuta deploy.sh)",
        })

    entries = await _get_provider_key_entries_admin(provider)
    if not entries:
        return JSONResponse({"keys": [], "no_key": True})
    global_model, key_models_saved = await get_provider_key_models_db(provider)
    keys_plain = [e["key"] for e in entries]
    available  = await _fetch_models_per_key(provider, keys_plain)
    db_rows    = {r["display_id"]: r for r in await list_db_keys(provider)}

    result = []
    for entry, models in zip(entries, available):
        kid = entry["id"]
        if kid >= KEY_ID_OFFSET:
            row = db_rows.get(kid, {})
            current_model = row.get("model") or global_model
        else:
            current_model = key_models_saved.get(str(kid), global_model)
        source, active = entry["source"], entry["active"]
        result.append({
            "index":         kid,
            "masked":        _mask_key(entry["key"]),
            "models":        models,
            "current_model": current_model,
            "source":        source,
            "active":        active,
        })
    return JSONResponse({"keys": result, "global_model": global_model})


# ── Fetchers por proveedor ─────────────────────────────────────────────────────

async def _fetch_models_per_key(provider: str, keys: list[str]) -> list[list[str]]:
    """Devuelve lista de modelos disponibles para cada key, en paralelo."""
    if provider == "groq":
        url     = "https://api.groq.com/openai/v1/models"
        auth_fn = lambda k: {"Authorization": f"Bearer {k}"}
        parse   = lambda d: sorted(set(m["id"] for m in d.get("data", []) if "whisper" in m["id"].lower()))
    elif provider in ("deepgram", "deepgramv2"):
        url     = "https://api.deepgram.com/v1/models"
        auth_fn = lambda k: {"Authorization": f"Token {k}"}
        parse   = lambda d: sorted(set(
            m["canonical_name"] for m in d.get("stt", [])
            if "es" in m.get("languages", []) or not m.get("languages")
        )) or ["nova-3-general", "nova-2-general", "nova-2", "nova-general", "enhanced-general"]
    elif provider == "fireworks":
        url     = "https://api.fireworks.ai/inference/v1/models"
        auth_fn = lambda k: {"Authorization": f"Bearer {k}"}
        parse   = lambda d: sorted(
            m["id"] for m in d.get("data", [])
            if "whisper" in m["id"].lower() or "audio" in m.get("object", "")
        ) or ["whisper-v3-turbo", "whisper-v3"]
    elif provider == "together":
        # Together AI no expone endpoint de modelos público — lista estática de modelos ASR
        return [[
            "openai/whisper-large-v3",
            "nvidia/parakeet-tdt-0.6b-v2",
            "openai/whisper-large-v3-turbo",
        ]] * len(keys)
    elif provider == "openai":
        # OpenAI modelos de transcripción disponibles
        return [[
            "gpt-4o-mini-transcribe",
            "gpt-4o-transcribe",
            "whisper-1",
        ]] * len(keys)
    else:
        return [[] for _ in keys]

    async with httpx.AsyncClient(timeout=6.0) as c:
        responses = await asyncio.gather(*[
            _query_one(c, url, auth_fn(k)) for k in keys
        ])
    fallback = parse({})
    return [parse(r) or fallback for r in responses]


async def _fetch_models(provider: str) -> list[str]:
    try:
        if provider == "groq":
            return await _fetch_groq_models()
        if provider in ("deepgram", "deepgramv2"):
            return await _fetch_deepgram_models()
        if provider == "fireworks":
            return await _fetch_fireworks_models()
    except Exception:
        pass
    return []


async def _query_one(c: httpx.AsyncClient, url: str, headers: dict) -> dict:
    try:
        r = await c.get(url, headers=headers)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


async def _fetch_groq_models() -> list[str]:
    keys = _all_keys(settings.GROQ_API_KEYS, settings.GROQ_API_KEY)
    if not keys:
        return []
    async with httpx.AsyncClient(timeout=6.0) as c:
        results = await asyncio.gather(*[
            _query_one(c, "https://api.groq.com/openai/v1/models",
                       {"Authorization": f"Bearer {k}"})
            for k in keys
        ])
    found: set[str] = set()
    for data in results:
        found.update(m["id"] for m in data.get("data", []) if "whisper" in m["id"].lower())
    return sorted(found)


async def _fetch_deepgram_models() -> list[str]:
    keys = _all_keys(settings.DEEPGRAM_API_KEYS, settings.DEEPGRAM_API_KEY)
    fallback = ["nova-2", "nova-2-general", "nova-2-phonecall", "nova-2-meeting",
                "nova", "enhanced", "base"]
    if not keys:
        return fallback
    async with httpx.AsyncClient(timeout=6.0) as c:
        results = await asyncio.gather(*[
            _query_one(c, "https://api.deepgram.com/v1/models",
                       {"Authorization": f"Token {k}"})
            for k in keys
        ])
    found: set[str] = set()
    for data in results:
        found.update(
            m["canonical_name"] for m in data.get("stt", [])
            if "es" in m.get("languages", []) or not m.get("languages")
        )
    return sorted(found) if found else fallback


async def _fetch_fireworks_models() -> list[str]:
    keys = _all_keys(settings.FIREWORKS_API_KEYS, settings.FIREWORKS_API_KEY)
    fallback = ["whisper-v3-turbo", "whisper-v3", "whisper-v2"]
    if not keys:
        return fallback
    async with httpx.AsyncClient(timeout=6.0) as c:
        results = await asyncio.gather(*[
            _query_one(c, "https://api.fireworks.ai/inference/v1/models",
                       {"Authorization": f"Bearer {k}"})
            for k in keys
        ])
    found: set[str] = set()
    for data in results:
        found.update(
            m["id"] for m in data.get("data", [])
            if "whisper" in m["id"].lower() or "audio" in m.get("object", "")
        )
    return sorted(found) if found else fallback


# ── Consumo / Stats ────────────────────────────────────────────────────────────

def _mask_api_keys_for_provider(provider: str, idx: int) -> str:
    """Devuelve la key enmascarada para mostrar en la UI."""
    try:
        if provider == "groq":
            keys = _settings.get_groq_keys()
        elif provider in ("deepgram", "deepgramv2"):
            keys = _settings.get_deepgram_keys()
        elif provider == "fireworks":
            keys = _settings.get_fireworks_keys()
        elif provider == "together":
            keys = _settings.get_together_keys()
        elif provider == "openai":
            keys = _settings.get_openai_keys()
        else:
            return f"KEY {idx + 1}"
        if idx < len(keys):
            k = keys[idx]
            return k[:6] + "…" + k[-4:]
        return f"KEY {idx + 1}"
    except Exception:
        return f"KEY {idx + 1}"


@router.get("/providers/stats", response_class=HTMLResponse)
async def stats_page(request: Request, period: int = 1):
    sess = get_session(request)
    if not sess:
        return login_redirect(request)
    admin_prefix = settings.ADMIN_PREFIX
    period = max(1, min(period, 90))
    summary  = await get_summary(period)
    by_key   = await get_by_key(period)

    # Agrupar by_key por proveedor para fácil acceso en template
    keys_by_provider: dict[str, list] = {}
    for row in by_key:
        p = row["provider"]
        row["masked"] = _mask_api_keys_for_provider(p, row["key_idx"])
        keys_by_provider.setdefault(p, []).append(row)

    total_cost = sum(r["cost_usd"] for r in summary)

    return templates.TemplateResponse(request, "provider_stats.html", {
        "request":          request,
        "admin_prefix":     admin_prefix,
        "active_page":      "stats",
        "summary":          summary,
        "keys_by_provider": keys_by_provider,
        "total_cost":       round(total_cost, 4),
        "period":           period,
    })


@router.get("/providers/stats/json")
async def stats_json(request: Request, period: int = 1):
    sess = get_session(request)
    if not sess:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    period  = max(1, min(period, 90))
    summary = await get_summary(period)
    by_key  = await get_by_key(period)
    for row in by_key:
        row["masked"] = _mask_api_keys_for_provider(row["provider"], row["key_idx"])
    return JSONResponse({"summary": summary, "by_key": by_key,
                         "total_cost": round(sum(r["cost_usd"] for r in summary), 4)})
