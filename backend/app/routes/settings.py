"""Settings CRUD endpoints - key/value store grouped by category."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import decrypt_value, encrypt_value, is_encrypted_field
from app.database import get_db
from app.models.settings import Setting
from app.models.schemas import (
    LLMConfig,
    SettingsBatchUpdateRequest,
    SettingsResponse,
    SettingsUpdateRequest,
    GPUStatusResponse,
    GPUToggleRequest,
    ModelConfig,
    LLMProvider,
    ActiveLLM,
    FetchModelsRequest,
)
from app.utils.secrets import (
    decrypt_value,
    encrypt_value,
    is_masked,
    is_sensitive_key,
    mask_value,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


# ------------------------------------------------------------------
# SSRF protection - allowed LLM service hosts
# ------------------------------------------------------------------


ALLOWED_LLM_HOSTS = {
    "api.openai.com",
    "api.anthropic.com",
    "generativelanguage.googleapis.com",
    "us-central1-aiplatform.googleapis.com",
    "localhost",
    "127.0.0.1",
    "::1",
}


def validate_llm_url(url: str) -> str:
    """Validate that a URL points to an allowed LLM service."""
    if not url:
        return url
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    allowed = False
    for pattern in ALLOWED_LLM_HOSTS:
        if pattern.startswith("*."):
            if hostname.endswith(pattern[1:]) or hostname == pattern[2:]:
                allowed = True
                break
        elif hostname == pattern:
            allowed = True
            break
    if not allowed:
        raise ValueError(
            f"URL hostname '{hostname}' is not in the allowed list. "
            f"Allowed: {', '.join(sorted(ALLOWED_LLM_HOSTS))}"
        )
    return url


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _decrypt_setting(row: Setting) -> str:
    """Decrypt a setting value if it's sensitive."""
    if is_sensitive_key(row.key):
        return decrypt_value(row.value)
    return row.value


def _masked_response(row: Setting) -> SettingsResponse:
    """Build a response, masking sensitive values."""
    value = _decrypt_setting(row)
    if is_sensitive_key(row.key):
        value = mask_value(value)
    return SettingsResponse(key=row.key, value=value, category=row.category)


# ------------------------------------------------------------------
# List all
# ------------------------------------------------------------------


@router.get("/", response_model=dict[str, list[SettingsResponse]])
async def list_settings(
    db: AsyncSession = Depends(get_db),
) -> dict[str, list[SettingsResponse]]:
    """Return all settings grouped by category."""
    stmt = select(Setting).order_by(Setting.category, Setting.key)
    result = await db.execute(stmt)
    rows = result.scalars().all()

    grouped: dict[str, list[SettingsResponse]] = defaultdict(list)
    for r in rows:
        grouped[r.category].append(_masked_response(r))
    return dict(grouped)


# ------------------------------------------------------------------
# Get one
# ------------------------------------------------------------------


@router.get("/key/{key}", response_model=SettingsResponse)
async def get_setting(
    key: str,
    db: AsyncSession = Depends(get_db),
) -> SettingsResponse:
    """Return a single setting by key."""
    stmt = select(Setting).where(Setting.key == key)
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"Setting '{key}' not found")
    return _masked_response(row)


# ------------------------------------------------------------------
# Upsert single
# ------------------------------------------------------------------


@router.put("/", response_model=SettingsResponse)
async def upsert_setting(
    body: SettingsUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> SettingsResponse:
    """Insert or update a single setting."""
    stmt = select(Setting).where(Setting.key == body.key)
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()

    save_value = body.value
    # If the value is masked (user didn't change it), keep existing encrypted value
    if is_encrypted_field(body.key) and is_masked(body.value) and row:
        save_value = row.value  # preserve existing encrypted value
    elif is_encrypted_field(body.key) and body.value:
        from app.core.api_manager import update_secret_cache
        update_secret_cache(body.key, body.value)
        save_value = encrypt_value(body.value)

    if row:
        row.value = save_value
        row.category = body.category
    else:
        row = Setting(key=body.key, value=save_value, category=body.category)
        db.add(row)

    await db.flush()
    return _masked_response(row)


# ------------------------------------------------------------------
# Batch upsert
# ------------------------------------------------------------------


@router.put("/batch", response_model=list[SettingsResponse])
async def batch_upsert(
    body: SettingsBatchUpdateRequest,
    db: AsyncSession = Depends(get_db),
) -> list[SettingsResponse]:
    """Upsert multiple settings at once."""
    results: list[SettingsResponse] = []
    for item in body.settings:
        stmt = select(Setting).where(Setting.key == item.key)
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()

        save_value = item.value
        if is_encrypted_field(item.key) and is_masked(item.value) and row:
            save_value = row.value
        elif is_encrypted_field(item.key) and item.value:
            from app.core.api_manager import update_secret_cache
            update_secret_cache(item.key, item.value)
            save_value = encrypt_value(item.value)

        if row:
            row.value = save_value
            row.category = item.category
        else:
            row = Setting(key=item.key, value=save_value, category=item.category)
            db.add(row)

        results.append(_masked_response(row))
    await db.flush()
    return results


# ------------------------------------------------------------------
# Delete
# ------------------------------------------------------------------


@router.delete("/key/{key}")
async def delete_setting(
    key: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Delete a single setting."""
    stmt = delete(Setting).where(Setting.key == key)
    await db.execute(stmt)
    return {"status": "deleted", "key": key}


# ------------------------------------------------------------------
# LLM config helpers
# ------------------------------------------------------------------

# Sensitive LLM config keys that need decryption/masking
_LLM_SENSITIVE_KEYS = {
    "gemini_api_key",
    "openai_api_key",
    "claude_api_key",
    "azure_api_key",
    "vertex_project_id",
}


@router.get("/llm/config", response_model=LLMConfig)
async def get_llm_config(
    db: AsyncSession = Depends(get_db),
) -> LLMConfig:
    """Return the full LLM configuration assembled from individual settings (legacy fallback)."""
    active = await get_active_llm(db)
    providers = await get_llm_providers(db)
    active_prov = next((p for p in providers if p.id == active.provider_id), None)

    data = {
        "llm_service": "no_llm" if active.provider_id == "none" else active.provider_id,
        "timeout": 60,
        "max_retries": 3,
        "max_output_tokens": 4096
    }

    if active_prov:
        api_key = active_prov.api_key
        model_id = active.model_id
        base_url = active_prov.base_url

        model_cfg = next((m for m in active_prov.models if m.model_id == model_id), None)
        if model_cfg:
            if model_cfg.timeout: data["timeout"] = model_cfg.timeout
            if model_cfg.max_retries: data["max_retries"] = model_cfg.max_retries
            if model_cfg.max_output_tokens: data["max_output_tokens"] = model_cfg.max_output_tokens

        p_type = active_prov.type
        if p_type == "gemini":
            data["gemini_api_key"] = api_key
            data["gemini_model_name"] = model_id
        elif p_type == "claude":
            data["claude_api_key"] = api_key
            data["claude_model_name"] = model_id
        elif p_type in ("openai", "custom_openai"):
            data["openai_api_key"] = api_key
            data["openai_base_url"] = base_url
            data["openai_model"] = model_id
        elif p_type == "ollama":
            data["ollama_base_url"] = base_url
            data["ollama_model"] = model_id
        elif p_type == "azure":
            data["azure_api_key"] = api_key
            data["azure_endpoint"] = base_url
            data["azure_deployment_name"] = model_id
        elif p_type == "vertex":
            data["vertex_project_id"] = api_key
            data["vertex_location"] = base_url
            data["gemini_model_name"] = model_id

    return LLMConfig(**{k: v for k, v in data.items() if k in LLMConfig.model_fields})


@router.put("/llm/config", response_model=LLMConfig)
async def save_llm_config(
    body: LLMConfig,
    db: AsyncSession = Depends(get_db),
) -> LLMConfig:
    """Persist the legacy LLM configuration by mapping it to new providers list."""
    service = body.llm_service
    provider_id = "none" if service == "no_llm" else service.value

    providers = await get_llm_providers(db)
    active_prov = next((p for p in providers if p.id == provider_id), None)

    model_id = ""
    if provider_id == "gemini":
        model_id = body.gemini_model_name or "gemini-2.0-flash"
        if active_prov:
            if body.gemini_api_key: active_prov.api_key = body.gemini_api_key
    elif provider_id == "claude":
        model_id = body.claude_model_name or "claude-3-7-sonnet-20250219"
        if active_prov:
            if body.claude_api_key: active_prov.api_key = body.claude_api_key
    elif provider_id == "openai":
        model_id = body.openai_model or "gpt-4o-mini"
        if active_prov:
            if body.openai_api_key: active_prov.api_key = body.openai_api_key
            if body.openai_base_url: active_prov.base_url = body.openai_base_url
    elif provider_id == "ollama":
        model_id = body.ollama_model or "llama3.2-vision"
        if active_prov:
            if body.ollama_base_url: active_prov.base_url = body.ollama_base_url
    elif provider_id == "azure":
        model_id = body.azure_deployment_name or "gpt-4o"
        if active_prov:
            if body.azure_api_key: active_prov.api_key = body.azure_api_key
            if body.azure_endpoint: active_prov.base_url = body.azure_endpoint
    elif provider_id == "vertex":
        model_id = body.gemini_model_name or "gemini-2.0-flash"
        if active_prov:
            if body.vertex_project_id: active_prov.api_key = body.vertex_project_id
            if body.vertex_location: active_prov.base_url = body.vertex_location

    if active_prov:
        model_cfg = next((m for m in active_prov.models if m.model_id == model_id), None)
        if not model_cfg:
            active_prov.models.append(ModelConfig(
                model_id=model_id,
                timeout=body.timeout,
                max_retries=body.max_retries,
                max_output_tokens=body.max_output_tokens
            ))
        else:
            model_cfg.timeout = body.timeout
            model_cfg.max_retries = body.max_retries
            model_cfg.max_output_tokens = body.max_output_tokens

    # Save changes using save_llm_providers (handles encrypt/mask/cache refresh)
    await save_llm_providers(providers, db)

    # Save global active
    await save_active_llm(ActiveLLM(provider_id=provider_id, model_id=model_id), db)

    return await get_llm_config(db)


# ------------------------------------------------------------------
# Test connection
# ------------------------------------------------------------------


@router.post("/llm/test")
async def test_llm_connection(
    body: LLMConfig,
) -> dict[str, Any]:
    """Test the LLM connection to the chosen service using provided credentials."""
    import httpx

    service = body.llm_service
    if service == "no_llm":
        return {"success": True, "message": "No LLM service configured - connection not tested."}

    timeout = httpx.Timeout(15.0, connect=5.0)

    try:
        if service == "gemini":
            api_key = body.gemini_api_key
            if not api_key:
                raise HTTPException(status_code=400, detail="Gemini API Key is required")

            if is_masked(api_key):
                api_key = "secret:gemini_api_key"
            else:
                from app.core.api_manager import update_secret_cache
                update_secret_cache("gemini_api_key", api_key)
                api_key = "secret:gemini_api_key"

            # Use header-based auth instead of query param to avoid key in logs/URL
            url = "https://generativelanguage.googleapis.com/v1beta/models"
            headers = {"x-goog-api-key": api_key}
            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.get(url, headers=headers)

            if res.status_code != 200:
                detail = res.json().get("error", {}).get("message", res.text)
                raise HTTPException(status_code=400, detail=f"Gemini API returned error: {detail}")

            return {"success": True, "message": "Successfully connected to Gemini API!"}

        elif service == "openai":
            api_key = body.openai_api_key
            if not api_key:
                raise HTTPException(status_code=400, detail="OpenAI API Key is required")

            if is_masked(api_key):
                api_key = "secret:openai_api_key"
            else:
                from app.core.api_manager import update_secret_cache
                update_secret_cache("openai_api_key", api_key)
                api_key = "secret:openai_api_key"

            base_url = body.openai_base_url or "https://api.openai.com/v1"
            validate_llm_url(base_url)
            base_url = base_url.rstrip("/")
            url = f"{base_url}/models"
            headers = {"Authorization": f"Bearer {api_key}"}

            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.get(url, headers=headers)

            if res.status_code != 200:
                detail = res.json().get("error", {}).get("message", res.text)
                raise HTTPException(status_code=400, detail=f"OpenAI API returned error: {detail}")

            return {"success": True, "message": "Successfully connected to OpenAI API!"}

        elif service == "claude":
            api_key = body.claude_api_key
            if not api_key:
                raise HTTPException(status_code=400, detail="Claude API Key is required")

            if is_masked(api_key):
                api_key = "secret:claude_api_key"
            else:
                from app.core.api_manager import update_secret_cache
                update_secret_cache("claude_api_key", api_key)
                api_key = "secret:claude_api_key"

            base_url = body.openai_base_url or "https://api.anthropic.com/v1"
            validate_llm_url(base_url)
            url = f"{base_url.rstrip('/')}/messages"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            data = {
                "model": body.claude_model_name or "claude-3-7-sonnet-20250219",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }

            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.post(url, headers=headers, json=data)

            if res.status_code in (401, 403):
                detail = res.json().get("error", {}).get("message", "Invalid API key")
                raise HTTPException(status_code=400, detail=f"Anthropic API returned auth error: {detail}")
            elif res.status_code not in (200, 400):
                detail = res.json().get("error", {}).get("message", res.text)
                raise HTTPException(status_code=400, detail=f"Anthropic API returned error: {detail}")

            return {"success": True, "message": "Successfully connected to Claude API!"}

        elif service == "ollama":
            base_url = body.ollama_base_url or "http://localhost:11434"
            validate_llm_url(base_url)
            base_url = base_url.rstrip("/")
            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.get(f"{base_url}/api/tags")

            if res.status_code != 200:
                raise HTTPException(status_code=400, detail=f"Ollama returned status {res.status_code}")

            return {"success": True, "message": f"Successfully connected to local Ollama server at {base_url}!"}

        elif service == "azure":
            api_key = body.azure_api_key
            endpoint = body.azure_endpoint
            if not api_key or not endpoint:
                raise HTTPException(status_code=400, detail="Azure API Key and Endpoint are required")

            if is_masked(api_key):
                api_key = "secret:azure_api_key"
            else:
                from app.core.api_manager import update_secret_cache
                update_secret_cache("azure_api_key", api_key)
                api_key = "secret:azure_api_key"

            validate_llm_url(endpoint)
            endpoint = endpoint.rstrip("/")
            url = f"{endpoint}/openai/models?api-version=2023-05-15"
            headers = {"api-key": api_key}

            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.get(url, headers=headers)

            if res.status_code != 200:
                raise HTTPException(status_code=400, detail=f"Azure OpenAI returned error: {res.text}")

            return {"success": True, "message": "Successfully connected to Azure OpenAI!"}

        elif service == "vertex":
            project_id = body.vertex_project_id
            if not project_id:
                raise HTTPException(status_code=400, detail="Vertex Project ID is required")

            if is_masked(project_id):
                project_id = "secret:vertex_project_id"
            else:
                from app.core.api_manager import update_secret_cache
                update_secret_cache("vertex_project_id", project_id)
                project_id = "secret:vertex_project_id"

            url = f"https://us-central1-aiplatform.googleapis.com/v1/projects/{project_id}/locations/us-central1/publishers/google/models"
            async with httpx.AsyncClient(timeout=timeout) as client:
                res = await client.get(url)
            if res.status_code == 404:
                raise HTTPException(status_code=400, detail="Vertex project not found or invalid URL path")
            return {"success": True, "message": "Vertex AI settings structured correctly (Authentication handled via environment credentials)."}

        else:
            raise HTTPException(status_code=400, detail=f"Unsupported service '{service}'")

    except ValueError as e:
        # URL validation error (SSRF)
        raise HTTPException(status_code=400, detail=str(e))
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail=f"Failed to connect to the {service} host. Verify internet connection and host URL.")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Connection request timed out. Please check network settings.")
    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        logger.error("Error testing LLM connection", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Connection test encountered an error: {str(e)}")


# ------------------------------------------------------------------
# Defaults
# ------------------------------------------------------------------


@router.get("/defaults", response_model=dict[str, Any])
async def get_defaults() -> dict[str, Any]:
    """Return marker-pdf's default configuration values."""
    from app.services.marker_service import MarkerService

    return MarkerService.get_defaults()


# ------------------------------------------------------------------
# GPU Acceleration
# ------------------------------------------------------------------

@router.get("/gpu/status", response_model=GPUStatusResponse)
async def get_gpu_status() -> GPUStatusResponse:
    """Get current GPU acceleration status, logs, and progress."""
    from app.services.gpu_service import gpu_service

    return GPUStatusResponse(**gpu_service.status_dict)


@router.post("/gpu/install", response_model=GPUStatusResponse)
async def install_gpu() -> GPUStatusResponse:
    """Trigger background installation of CUDA-enabled PyTorch."""
    from app.services.gpu_service import gpu_service

    gpu_service.start_install()
    return GPUStatusResponse(**gpu_service.status_dict)


@router.post("/gpu/toggle")
async def toggle_gpu(
    body: GPUToggleRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Save the GPU acceleration enabled preference in settings database."""
    stmt = select(Setting).where(Setting.key == "gpu_acceleration_enabled")
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()

    val = "true" if body.enabled else "false"
    if row:
        row.value = val
        row.category = "gpu"
    else:
        db.add(Setting(key="gpu_acceleration_enabled", value=val, category="gpu"))
    await db.commit()
    return {"status": "success", "enabled": body.enabled}


# ------------------------------------------------------------------
# LLM Providers Configuration & Endpoints
# ------------------------------------------------------------------

async def init_llm_providers_if_missing(db: AsyncSession) -> None:
    """Ensure LLM providers and active LLM configuration exist in the database."""
    stmt = select(Setting).where(Setting.key == "llm_providers")
    res = await db.execute(stmt)
    row = res.scalar_one_or_none()

    # Load legacy settings if present
    stmt = select(Setting).where(Setting.category == "llm")
    res = await db.execute(stmt)
    rows = res.scalars().all()
    old_settings = {r.key: r.value for r in rows}

    def get_old(key: str, default: Any = "") -> Any:
        val = old_settings.get(key, default)
        if key in _LLM_SENSITIVE_KEYS and val:
            val = decrypt_value(val)
        return val

    timeout = int(get_old("timeout", 60) or 60)
    max_retries = int(get_old("max_retries", 3) or 3)
    max_output = int(get_old("max_output_tokens", 4096) or 4096)

    default_providers = [
        {
            "id": "gemini",
            "type": "gemini",
            "label": "Gemini",
            "api_key": encrypt_value(get_old("gemini_api_key")) if get_old("gemini_api_key") else None,
            "fallback_api_keys": [],
            "concurrency": 4,
            "models": [
                {"model_id": get_old("gemini_model_name") or "gemini-2.0-flash", "timeout": timeout, "max_retries": max_retries, "max_output_tokens": max_output, "vision_capable": False},
                {"model_id": "gemini-2.0-pro-exp-02-05", "timeout": timeout, "max_retries": max_retries, "max_output_tokens": max_output, "vision_capable": False}
            ]
        },
        {
            "id": "claude",
            "type": "claude",
            "label": "Anthropic",
            "api_key": encrypt_value(get_old("claude_api_key")) if get_old("claude_api_key") else None,
            "fallback_api_keys": [],
            "concurrency": 4,
            "models": [
                {"model_id": get_old("claude_model_name") or "claude-3-7-sonnet-20250219", "timeout": timeout, "max_retries": max_retries, "max_output_tokens": max_output, "vision_capable": False},
                {"model_id": "claude-3-5-sonnet-20241022", "timeout": timeout, "max_retries": max_retries, "max_output_tokens": max_output, "vision_capable": False}
            ]
        },
        {
            "id": "openai",
            "type": "openai",
            "label": "OpenAI",
            "api_key": encrypt_value(get_old("openai_api_key")) if get_old("openai_api_key") else None,
            "base_url": get_old("openai_base_url") or "https://api.openai.com/v1",
            "fallback_api_keys": [],
            "concurrency": 4,
            "models": [
                {"model_id": get_old("openai_model") or "gpt-4o-mini", "timeout": timeout, "max_retries": max_retries, "max_output_tokens": max_output, "vision_capable": False},
                {"model_id": "gpt-4o", "timeout": timeout, "max_retries": max_retries, "max_output_tokens": max_output, "vision_capable": False}
            ]
        },
        {
            "id": "ollama",
            "type": "ollama",
            "label": "Ollama",
            "base_url": get_old("ollama_base_url") or "http://localhost:11434",
            "fallback_api_keys": [],
            "concurrency": 2,
            "models": [
                {"model_id": get_old("ollama_model") or "llama3.2-vision", "timeout": timeout, "max_retries": max_retries, "max_output_tokens": max_output, "vision_capable": False}
            ]
        },
        {
            "id": "azure",
            "type": "azure",
            "label": "Azure OpenAI",
            "api_key": encrypt_value(get_old("azure_api_key")) if get_old("azure_api_key") else None,
            "base_url": get_old("azure_endpoint") or "",
            "fallback_api_keys": [],
            "concurrency": 4,
            "models": [
                {"model_id": get_old("azure_deployment_name") or "gpt-4o", "timeout": timeout, "max_retries": max_retries, "max_output_tokens": max_output, "vision_capable": False}
            ]
        },
        {
            "id": "vertex",
            "type": "vertex",
            "label": "Vertex AI",
            "api_key": encrypt_value(get_old("vertex_project_id")) if get_old("vertex_project_id") else None,
            "base_url": get_old("vertex_location") or "us-central1",
            "fallback_api_keys": [],
            "concurrency": 4,
            "models": [
                {"model_id": get_old("gemini_model_name") or "gemini-2.0-flash", "timeout": timeout, "max_retries": max_retries, "max_output_tokens": max_output, "vision_capable": False}
            ]
        }
    ]

    existing_providers = []
    if row:
        try:
            existing_providers = json.loads(row.value)
        except Exception:
            pass

    existing_ids = {p["id"] for p in existing_providers if "id" in p}
    needs_save = False

    if not row:
        existing_providers = default_providers
        needs_save = True
    else:
        for def_p in default_providers:
            if def_p["id"] not in existing_ids:
                existing_providers.append(def_p)
                needs_save = True

    if needs_save:
        serialized = json.dumps(existing_providers)
        if row:
            row.value = serialized
        else:
            db.add(Setting(key="llm_providers", value=serialized, category="llm"))
        await db.flush()

    active_stmt = select(Setting).where(Setting.key == "llm_global_active")
    active_res = await db.execute(active_stmt)
    active_row = active_res.scalar_one_or_none()

    if not active_row:
        active_service = get_old("llm_service") or "no_llm"
        if active_service == "no_llm":
            active_provider = "none"
            active_model = ""
        else:
            active_provider = active_service
            prov_obj = next((p for p in existing_providers if p["id"] == active_provider), None)
            active_model = prov_obj["models"][0]["model_id"] if prov_obj and prov_obj["models"] else ""

        active_data = {"provider_id": active_provider, "model_id": active_model}
        db.add(Setting(key="llm_global_active", value=json.dumps(active_data), category="llm"))

    await db.commit()

    # Keep image-understanding defaults seeded alongside LLM providers so every
    # call site that guarantees LLM config also guarantees image config.
    await init_image_settings_if_missing(db)


async def init_image_settings_if_missing(db: AsyncSession) -> None:
    """Ensure image-understanding default settings exist in the database.

    Seeds two keys under the ``image`` category:
        - ``vlm_model`` (default ""): overrides the auto-resolved vision model.
          Empty string means "auto-resolve to the first vision-capable model".
        - ``max_images_per_doc`` (default "50"): caps VLM work per document.

    Values are stored as strings (the settings store is a generic key/value
    table). Only inserts when the key is absent; never overwrites user edits.
    """
    defaults = {
        "vlm_model": "",
        "max_images_per_doc": "50",
    }
    for key, value in defaults.items():
        stmt = select(Setting).where(Setting.key == key)
        existing = (await db.execute(stmt)).scalar_one_or_none()
        if existing is None:
            db.add(Setting(key=key, value=value, category="image"))
    await db.flush()


async def fetch_models_from_api(
    provider_type: str,
    base_url: str | None,
    api_key: str | None,
) -> list[str]:
    """Fetch available models list from a provider API endpoint."""
    import httpx
    timeout = httpx.Timeout(10.0, connect=3.0)

    if provider_type in ("openai", "custom_openai"):
        if not base_url:
            base_url = "https://api.openai.com/v1"
        url = f"{base_url.rstrip('/')}/models"
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.get(url, headers=headers)
        res.raise_for_status()
        data = res.json().get("data", [])
        return sorted([m["id"] for m in data if "id" in m])

    elif provider_type == "gemini":
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        params = {}
        if api_key:
            params["key"] = api_key
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.get(url, params=params)
        res.raise_for_status()
        models = res.json().get("models", [])
        return sorted([m["name"].split("/")[-1] for m in models if "name" in m])

    elif provider_type in ("claude", "custom_anthropic"):
        url_base = base_url or "https://api.anthropic.com/v1"
        url = f"{url_base.rstrip('/')}/models"
        headers = {
            "anthropic-version": "2023-06-01",
        }
        if api_key:
            headers["x-api-key"] = api_key
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.get(url, headers=headers)
        res.raise_for_status()
        data = res.json().get("data", [])
        return sorted([m["id"] for m in data if "id" in m])

    elif provider_type == "ollama":
        if not base_url:
            base_url = "http://localhost:11434"
        url = f"{base_url.rstrip('/')}/api/tags"
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.get(url)
        res.raise_for_status()
        models = res.json().get("models", [])
        model_ids = []
        for m in models:
            if "name" in m:
                model_ids.append(m["name"])
            elif "model" in m:
                model_ids.append(m["model"])
        return sorted(model_ids)

    elif provider_type == "azure":
        if not base_url:
            return []
        url = f"{base_url.rstrip('/')}/openai/deployments?api-version=2023-05-15"
        headers = {}
        if api_key:
            headers["api-key"] = api_key
        async with httpx.AsyncClient(timeout=timeout) as client:
            res = await client.get(url, headers=headers)
        res.raise_for_status()
        deployments = res.json().get("data", [])
        return sorted([d["id"] for d in deployments if "id" in d])

    elif provider_type == "vertex":
        import asyncio
        from google import genai

        def _list_vertex():
            client = genai.Client(
                vertexai=True,
                project=api_key,  # project_id is passed as api_key
                location=base_url or "us-central1",
            )
            models_pager = client.models.list()
            model_ids = []
            for m in models_pager:
                name = m.name.split("/")[-1]
                model_ids.append(name)
            return sorted(list(set(model_ids)))

        return await asyncio.to_thread(_list_vertex)

    return []


@router.get("/llm/providers", response_model=list[LLMProvider])
async def get_llm_providers(
    db: AsyncSession = Depends(get_db),
) -> list[LLMProvider]:
    """Get all configured LLM providers (keys masked)."""
    stmt = select(Setting).where(Setting.key == "llm_providers")
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()

    if not row:
        await init_llm_providers_if_missing(db)
        stmt = select(Setting).where(Setting.key == "llm_providers")
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()

    providers = []
    if row:
        providers = json.loads(row.value)

    masked_list = []
    for p in providers:
        masked_list.append(LLMProvider(
            id=p["id"],
            type=p["type"],
            label=p["label"],
            base_url=p.get("base_url"),
            concurrency=p.get("concurrency"),
            api_key=mask_value(decrypt_value(p["api_key"])) if p.get("api_key") else None,
            fallback_api_keys=[mask_value(decrypt_value(fb)) for fb in p.get("fallback_api_keys", [])],
            models=[ModelConfig(**m) for m in p.get("models", [])]
        ))
    return masked_list


@router.put("/llm/providers", response_model=list[LLMProvider])
async def save_llm_providers(
    body: list[LLMProvider],
    db: AsyncSession = Depends(get_db),
) -> list[LLMProvider]:
    """Save the LLM providers list, encrypting sensitive keys."""
    stmt = select(Setting).where(Setting.key == "llm_providers")
    result = await db.execute(stmt)
    existing_row = result.scalar_one_or_none()

    existing_providers = []
    if existing_row:
        try:
            existing_providers = json.loads(existing_row.value)
        except Exception:
            pass

    # Keep the STORED CIPHERTEXT verbatim. A masked (unchanged) key must be
    # preserved exactly as it sits in the DB - never decrypted-then-re-encrypted.
    # Round-tripping is what corrupts data: if decrypt_value fails on a
    # key/ENCRYPTION_KEY mismatch it fail-softs by returning the ciphertext
    # unchanged, and re-encrypting that yields a double-wrapped token whose inner
    # value is itself a Fernet token, not the real secret (HTTP 400 at call time).
    existing_map = {}
    for p in existing_providers:
        pid = p.get("id")
        if not pid:
            continue
        existing_map[pid] = {
            "api_key": p.get("api_key"),
            "fallback_api_keys": list(p.get("fallback_api_keys", [])),
        }

    updated_providers = []
    for p in body:
        pid = p.id
        p_type = p.type
        p_label = p.label
        base_url = p.base_url
        models = [m.model_dump() for m in p.models]

        # Encrypt only freshly-entered plaintext. Masked => reuse stored cipher.
        incoming_api_key = p.api_key
        if incoming_api_key and is_masked(incoming_api_key):
            encrypted_api_key = existing_map.get(pid, {}).get("api_key")
        elif incoming_api_key:
            encrypted_api_key = encrypt_value(incoming_api_key)
        else:
            encrypted_api_key = None

        encrypted_fallback_keys = []
        existing_fallbacks = existing_map.get(pid, {}).get("fallback_api_keys", [])
        for i, fb in enumerate(p.fallback_api_keys):
            if fb and is_masked(fb):
                if i < len(existing_fallbacks):
                    encrypted_fallback_keys.append(existing_fallbacks[i])
            elif fb:
                encrypted_fallback_keys.append(encrypt_value(fb))

        updated_providers.append({
            "id": pid,
            "type": p_type,
            "label": p_label,
            "api_key": encrypted_api_key,
            "fallback_api_keys": encrypted_fallback_keys,
            "base_url": base_url,
            "concurrency": p.concurrency,
            "models": models
        })

    serialized = json.dumps(updated_providers)
    if existing_row:
        existing_row.value = serialized
    else:
        db.add(Setting(key="llm_providers", value=serialized, category="llm"))

    await db.flush()
    await db.commit()

    from app.core.api_manager import load_secrets_from_db
    await load_secrets_from_db()

    masked_list = []
    for p in updated_providers:
        masked_list.append(LLMProvider(
            id=p["id"],
            type=p["type"],
            label=p["label"],
            base_url=p["base_url"],
            concurrency=p.get("concurrency"),
            api_key=mask_value(decrypt_value(p["api_key"])) if p["api_key"] else None,
            fallback_api_keys=[mask_value(decrypt_value(fb)) for fb in p["fallback_api_keys"]],
            models=[ModelConfig(**m) for m in p["models"]]
        ))
    return masked_list


@router.get("/llm/active", response_model=ActiveLLM)
async def get_active_llm(
    db: AsyncSession = Depends(get_db),
) -> ActiveLLM:
    """Get the active global LLM settings."""
    stmt = select(Setting).where(Setting.key == "llm_global_active")
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()

    if not row:
        await init_llm_providers_if_missing(db)
        stmt = select(Setting).where(Setting.key == "llm_global_active")
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()

    if row:
        data = json.loads(row.value)
        return ActiveLLM(**data)

    return ActiveLLM(provider_id="none", model_id="")


@router.put("/llm/active", response_model=ActiveLLM)
async def save_active_llm(
    body: ActiveLLM,
    db: AsyncSession = Depends(get_db),
) -> ActiveLLM:
    """Save the globally active LLM and sync with legacy settings."""
    stmt = select(Setting).where(Setting.key == "llm_global_active")
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()

    serialized = body.model_dump_json()
    if row:
        row.value = serialized
    else:
        db.add(Setting(key="llm_global_active", value=serialized, category="llm"))

    legacy_service = "no_llm" if body.provider_id == "none" else body.provider_id
    legacy_service_stmt = select(Setting).where(Setting.key == "llm_service")
    legacy_service_row = (await db.execute(legacy_service_stmt)).scalar_one_or_none()
    if legacy_service_row:
        legacy_service_row.value = legacy_service
    else:
        db.add(Setting(key="llm_service", value=legacy_service, category="llm"))

    await db.flush()
    await db.commit()
    return body


@router.post("/llm/providers/fetch-models")
async def fetch_provider_models(
    body: FetchModelsRequest,
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """Fetch model options directly from a provider's endpoint."""
    api_key = body.api_key
    base_url = body.base_url

    if body.provider_id and (not api_key or is_masked(api_key) or not base_url):
        stmt = select(Setting).where(Setting.key == "llm_providers")
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()
        if row:
            providers = json.loads(row.value)
            prov = next((p for p in providers if p["id"] == body.provider_id), None)
            if prov:
                if not api_key or is_masked(api_key):
                    api_key = decrypt_value(prov.get("api_key")) if prov.get("api_key") else None
                if not base_url:
                    base_url = prov.get("base_url")

    try:
        models = await fetch_models_from_api(body.type, base_url, api_key)
        return models
    except Exception as e:
        logger.error("Error fetching models from provider: %s", e, exc_info=True)
        raise HTTPException(status_code=400, detail=f"Failed to fetch models: {str(e)}")



