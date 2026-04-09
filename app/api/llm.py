"""LLM provider and model group API endpoints."""

import json

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.config import load_all_provider_catalogs, load_provider_catalog
from app.storage.dynamic_config_storage import DynamicConfigStorage
from app.lib.llm.factory import SUPPORTED_LLM_PROVIDERS
from app.utils.logger import logger


def get_llm_routes(config_storage: DynamicConfigStorage) -> list[Route]:

    async def handle_list_providers(request: Request) -> JSONResponse:
        """List all available LLM providers with their config status."""
        profile = getattr(request.user, "username", "admin")
        catalogs = load_all_provider_catalogs()
        providers = []

        for name in SUPPORTED_LLM_PROVIDERS:
            catalog = catalogs.get(name, {})
            provider_info = catalog.get("provider", {})

            requires_key = provider_info.get("requires_api_key", False)
            requires_sa = provider_info.get("requires_service_account", False)
            config_fields = provider_info.get("config_fields", {})

            # Build config_fields status and current_values
            config_fields_status = {}
            current_values = {}
            for field_key, field_spec in config_fields.items():
                full_key = f"{name}.{field_key}"
                val = config_storage.get("llm_config", full_key, profile=profile)
                config_fields_status[field_key] = {
                    "description": field_spec.get("description", ""),
                    "type": field_spec.get("type", "string"),
                    "secret": field_spec.get("secret", False),
                    "required": field_spec.get("required", True),
                    "default": field_spec.get("default"),
                    "configured": bool(val),
                }
                if val and not field_spec.get("secret", False):
                    current_values[field_key] = val

            # Determine configured status
            configured = True
            if config_fields:
                configured = all(
                    bool(config_storage.get("llm_config", f"{name}.{fk}", profile=profile))
                    for fk, fs in config_fields.items()
                    if fs.get("required", True)
                )
            elif requires_key:
                api_key = config_storage.get("llm_config", f"{name}.api_key", profile=profile)
                if not api_key:
                    from app.config.settings import BaseConfig
                    api_key = BaseConfig.get_provider_api_key(name, profile=profile)
                configured = bool(api_key)
            elif requires_sa:
                sa = config_storage.get("llm_config", f"{name}.service_account", profile=profile)
                configured = bool(sa)

            provider_entry = {
                "name": name,
                "display_name": provider_info.get("display_name", name),
                "requires_api_key": requires_key,
                "requires_service_account": requires_sa,
                "configured": configured,
                "model_count": len(catalog.get("models", [])),
            }
            if config_fields_status:
                provider_entry["config_fields"] = config_fields_status
                provider_entry["current_values"] = current_values

            providers.append(provider_entry)

        return JSONResponse({"providers": providers})

    async def handle_get_provider_models(request: Request) -> JSONResponse:
        """List models for a specific provider from the TOML catalog."""
        provider_name = request.path_params["name"]
        catalog = load_provider_catalog(provider_name)

        if not catalog:
            return JSONResponse(
                {"error": f"Provider '{provider_name}' not found"},
                status_code=404,
            )

        return JSONResponse({
            "provider": catalog.get("provider", {}),
            "models": catalog.get("models", []),
        })

    async def handle_update_provider(request: Request) -> JSONResponse:
        """Update provider configuration (API key, etc.)."""
        profile = getattr(request.user, "username", "admin")
        provider_name = request.path_params["name"]

        if provider_name not in SUPPORTED_LLM_PROVIDERS:
            return JSONResponse(
                {"error": f"Unknown provider: {provider_name}"},
                status_code=404,
            )

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        # Resolve secret flags from config_fields metadata
        catalog = load_provider_catalog(provider_name)
        config_fields = catalog.get("provider", {}).get("config_fields", {}) if catalog else {}

        # Save each config key
        for key, value in body.items():
            full_key = f"{provider_name}.{key}"
            field_spec = config_fields.get(key, {})
            is_secret = field_spec.get("secret", False) or "api_key" in key or "service_account" in key
            str_value = value if isinstance(value, str) else json.dumps(value)
            config_storage.set("llm_config", full_key, str_value, is_secret=is_secret, profile=profile)

        return JSONResponse({"success": True})

    async def handle_get_model_groups(request: Request) -> JSONResponse:
        """Get current model group assignments (high/low) and their reasoning_effort."""
        profile = getattr(request.user, "username", "admin")
        from app.config import settings as dynaconf_settings

        groups = {}
        reasoning_efforts: dict[str, str | None] = {}
        for group in ("high", "low"):
            # SQLite first
            val = config_storage.get("llm_config", f"model_group.{group}", profile=profile)
            if not val:
                try:
                    val = dynaconf_settings.get(f"llm.model_groups.{group}")
                except Exception:
                    val = None
            groups[group] = val or ""

            # Reasoning effort per group
            re_val = config_storage.get("llm_config", f"model_group.{group}.reasoning_effort", profile=profile)
            reasoning_efforts[group] = re_val or None

        # Also return default provider
        default_provider = config_storage.get("llm_config", "default_provider", profile=profile)
        if not default_provider:
            try:
                default_provider = dynaconf_settings.get("llm.default_provider", "")
            except Exception:
                default_provider = ""

        return JSONResponse({
            "model_groups": groups,
            "default_provider": default_provider,
            "reasoning_efforts": reasoning_efforts,
        })

    async def handle_update_model_groups(request: Request) -> JSONResponse:
        """Update model group assignments."""
        profile = getattr(request.user, "username", "admin")
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        model_groups = body.get("model_groups", {})
        for group, value in model_groups.items():
            if group in ("high", "low"):
                config_storage.set("llm_config", f"model_group.{group}", str(value), profile=profile)

        # Save reasoning_effort per group
        reasoning_efforts = body.get("reasoning_efforts", {})
        for group, value in reasoning_efforts.items():
            if group in ("high", "low"):
                if value:
                    config_storage.set("llm_config", f"model_group.{group}.reasoning_effort", str(value), profile=profile)
                else:
                    config_storage.delete("llm_config", f"model_group.{group}.reasoning_effort", profile=profile)

        default_provider = body.get("default_provider")
        if default_provider:
            config_storage.set("llm_config", "default_provider", default_provider, profile=profile)

        return JSONResponse({"success": True})

    return [
        Route("/api/llm/providers", handle_list_providers, methods=["GET"]),
        Route("/api/llm/providers/{name}/models", handle_get_provider_models, methods=["GET"]),
        Route("/api/llm/providers/{name}", handle_update_provider, methods=["PUT"]),
        Route("/api/llm/model-groups", handle_get_model_groups, methods=["GET"]),
        Route("/api/llm/model-groups", handle_update_model_groups, methods=["PUT"]),
    ]
