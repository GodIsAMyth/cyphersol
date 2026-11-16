"""CLI entrypoint for launching the offline CypherEdge & CypherTrace web platform."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
import urllib.request
import webbrowser

from cyphersol.llm_agents import (
    CLOUD_OPT_IN_ENV,
    PROVIDER_CATALOG,
    VALID_EXECUTION_MODES,
    LLMConfig,
    LLMProviderClient,
    LLMProviderError,
)
from cyphersol.server import ALLOWED_HOSTS_ENV, is_loopback_host, run_server
from cyphersol.storage import CypherVault


def _is_cyphersol_running(host: str, port: int) -> bool:
    """Return True if an active CypherSOL instance responds on host:port."""
    check_host = "127.0.0.1" if host == "0.0.0.0" else host
    health_url = f"http://{check_host}:{port}/api/health"
    try:
        with urllib.request.urlopen(health_url, timeout=1.2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("status") == "ok" and "CypherSOL" in str(data.get("engine", ""))
    except Exception:
        return False


def _prompt_secret(label: str) -> str:
    try:
        return getpass.getpass(label).strip()
    except Exception:
        return input(label).strip()


def _run_deployment_llm_wizard(db_path: str, force: bool = False) -> None:
    """Interactive deployment configuration wizard for runtime mode, provider, models, and API keys."""
    vault = CypherVault(db_path)
    existing = vault.get_llm_settings()
    if existing.get("configured") and not force:
        return

    print("\n" + "=" * 68)
    print("CypherSOL Deployment Setup — AI Agents (Stages 1–4) & Runtime Mode")
    print("=" * 68)
    print("Choose default runtime extraction mode:")
    print("  [1] Heuristics Only (default)        (No external API calls; statement data stays on this machine)")
    print("  [2] Hybrid: Heuristics + LLM Backup  (Sends failing rows / scans to the cloud AI provider you choose)")
    print("  [3] LLM Agents Primary (Stages 1–4)  (Sends statement content to the cloud AI provider you choose)")
    mode_choice = input("Select [1/2/3] (default 1): ").strip() or "1"
    mode_map = {"1": "heuristics", "2": "both", "3": "llm"}
    exec_mode = mode_map.get(mode_choice, "heuristics")

    cur_cfg = LLMConfig.from_dict(existing)
    provider = cur_cfg.provider
    model = cur_cfg.model
    vision_model = cur_cfg.vision_model
    # Only vault-held keys are carried forward; environment keys are never written to the vault.
    api_keys = cur_cfg.stored_api_keys()

    setup_ai = exec_mode in ("both", "llm")
    if exec_mode == "heuristics":
        ans = input("Configure a backup LLM API key anyway for later use? [y/N]: ").strip().lower()
        setup_ai = ans in {"y", "yes"}

    if setup_ai:
        print("\nSelect AI Provider (supports Multimodal Vision + JSON):")
        print("  [1] Google Gemini      (gemini-2.5-flash / gemini-2.5-pro)")
        print("  [2] Anthropic Claude   (claude-sonnet-4-20250514 / claude-3-7-sonnet-latest)")
        print("  [3] OpenAI             (gpt-4o / gpt-4o-mini)")
        prov_choice = input("Select [1/2/3] (default 1): ").strip() or "1"
        prov_map = {"1": "gemini", "2": "claude", "3": "openai"}
        provider = prov_map.get(prov_choice, "gemini")
        p_meta = PROVIDER_CATALOG[provider]

        model_in = input(f"Text/Reasoning Model for Stages 2–4 [{p_meta['default_model']}]: ").strip()
        model = model_in or p_meta["default_model"]

        vision_in = input(f"Vision Model for Stage 1 Scanned PDFs [{p_meta['default_vision_model']}]: ").strip()
        vision_model = vision_in or p_meta["default_vision_model"]

        existing_key = api_keys.get(provider, "")
        hint = " (press Enter to keep existing key)" if existing_key else f" ({p_meta['key_hint']})"
        key_in = _prompt_secret(f"{p_meta['label']} API Key{hint}: ")
        if key_in:
            api_keys[provider] = key_in

        if api_keys.get(provider):
            test_now = input("Test LLM API connection right now? [Y/n]: ").strip().lower() or "y"
            if test_now in {"y", "yes"}:
                probe_cfg = LLMConfig(
                    execution_mode=exec_mode,
                    provider=provider,
                    model=model,
                    vision_model=vision_model,
                    api_key=api_keys[provider],
                    api_keys=api_keys,
                    configured=True,
                )
                try:
                    res = LLMProviderClient(probe_cfg).test_connection()
                    print(f"  [OK] Connected to {p_meta['label']} ({model}) in {res['latency_ms']} ms.")
                except LLMProviderError as exc:
                    print(f"  [WARN] Connection test failed: {exc}")
                    print("  You can update or re-test keys anytime in the UI -> 'AI Models & Keys'.")

    new_cfg = LLMConfig(
        execution_mode=exec_mode,
        provider=provider,
        model=model,
        vision_model=vision_model,
        api_key=api_keys.get(provider, ""),
        api_keys=api_keys,
        configured=True,
        disabled_env_providers=list(cur_cfg.disabled_env_providers),
    )
    vault.save_llm_settings(new_cfg.to_storage_dict())
    print(f"\nSaved runtime settings locally in vault ({os.path.abspath(db_path)}).")
    if new_cfg.has_valid_key() and exec_mode != "heuristics":
        print(f"Cloud AI is ENABLED: statement content will be sent to {PROVIDER_CATALOG[provider]['label']}.\n")
    else:
        print("Cloud AI is disabled for the default mode.\n")


def _describe_cloud_status(db_path: str) -> str:
    cfg = LLMConfig.from_dict(CypherVault(db_path).get_llm_settings())
    if not cfg.cloud_egress_possible():
        return "disabled (heuristics only; no cloud AI key active)"
    return (
        f"ENABLED - {PROVIDER_CATALOG[cfg.provider]['label']} key from {cfg.key_source()} "
        f"(default mode: {cfg.effective_execution_mode()}); statement data may be sent to this provider"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CypherSOL — Offline & Hybrid LLM Bank Statement Analyzer (CypherEdge) & Fund Trail Intelligence (CypherTrace)"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--db", default="data/cyphersol_vault.db", help="Path to local SQLite vault")
    parser.add_argument("--samples", default="sample_statements", help="Directory for sample statements")
    parser.add_argument(
        "--open-browser",
        action="store_true",
        default=False,
        help="Open UI in default browser (enabled by default unless --no-browser is set)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        default=False,
        help="Do not automatically open the web browser on startup (for headless/service mode)",
    )
    parser.add_argument(
        "--configure-llm",
        action="store_true",
        default=False,
        help="Run interactive deployment wizard for Gemini / Claude / OpenAI models and API keys",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        default=False,
        help="Skip first-run interactive deployment prompt",
    )
    parser.add_argument(
        "--execution-mode",
        choices=list(VALID_EXECUTION_MODES),
        default=None,
        help="Override default runtime extraction mode ('heuristics', 'both', or 'llm')",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("CYPHERSOL_LOG_LEVEL", "INFO").upper(),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    should_open_browser = not args.no_browser
    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)

    if args.configure_llm or (not args.non_interactive and sys.stdin.isatty()):
        _run_deployment_llm_wizard(args.db, force=args.configure_llm)

    if args.execution_mode:
        vault = CypherVault(args.db)
        cfg = LLMConfig.from_dict(vault.get_llm_settings())
        cfg.execution_mode = args.execution_mode
        cfg.configured = True
        # to_storage_dict() excludes environment-sourced keys, so this never persists an exported key.
        vault.save_llm_settings(cfg.to_storage_dict())

    if not is_loopback_host(args.host) and args.host not in ("0.0.0.0", "::"):
        print(
            f"WARNING: binding to non-loopback host {args.host}. Requests are only accepted for loopback Host "
            f"names unless {args.host} is listed in {ALLOWED_HOSTS_ENV}. The API has no user authentication; "
            "do not expose it to untrusted networks."
        )
    elif args.host in ("0.0.0.0", "::"):
        print(
            "WARNING: listening on all interfaces. Only loopback Host names are accepted; the API has no user "
            "authentication. Prefer --host 127.0.0.1."
        )

    server = None
    try:
        server = run_server(host=args.host, port=args.port, db_path=args.db, sample_dir=args.samples)
    except OSError as exc:
        if _is_cyphersol_running(args.host, args.port):
            browser_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
            url = f"http://{args.host}:{args.port}/"
            print(f"CypherSOL is already running at {url}")
            if should_open_browser:
                print("Opening your browser to the active CypherSOL workspace...")
                webbrowser.open(f"http://{browser_host}:{args.port}/")
            return
        last_exc: OSError = exc
        for candidate_port in range(args.port + 1, args.port + 11):
            try:
                server = run_server(
                    host=args.host,
                    port=candidate_port,
                    db_path=args.db,
                    sample_dir=args.samples,
                )
                print(f"Port {args.port} was busy; switched automatically to port {candidate_port}.")
                args.port = candidate_port
                break
            except OSError as next_exc:
                last_exc = next_exc
        if server is None:
            raise last_exc

    url = f"http://{args.host}:{args.port}/"
    browser_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    browser_url = f"http://{browser_host}:{args.port}/"
    print("=" * 68)
    print(f"CypherSOL running at {url} (local only)")
    print(f"Vault DB:          {os.path.abspath(args.db)}")
    print(f"Sample Statements: {os.path.abspath(args.samples)}")
    print(f"Cloud AI:          {_describe_cloud_status(args.db)}")
    if os.environ.get(CLOUD_OPT_IN_ENV, "").strip() not in ("1", "true", "yes"):
        print(f"                   (environment API keys are ignored unless {CLOUD_OPT_IN_ENV}=1)")
    print("Press Ctrl + C in this window to stop the server.")
    print("=" * 68)
    if should_open_browser:
        webbrowser.open(browser_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down CypherSOL server.")
        server.server_close()


if __name__ == "__main__":
    main()
