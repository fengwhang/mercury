"""``mercury setup`` subcommand parser.

Extracted verbatim from ``mercury_cli/main.py:main()`` (god-file Phase 2).
Handler injected to avoid importing ``main``.
"""

from __future__ import annotations

from typing import Callable


def build_setup_parser(subparsers, *, cmd_setup: Callable) -> None:
    """Attach the ``setup`` subcommand to ``subparsers``."""
    # =========================================================================
    # setup command
    # =========================================================================
    setup_parser = subparsers.add_parser(
        "setup",
        help="Interactive setup wizard",
        description="Configure Mercury with an interactive wizard. "
        "Run a specific section: "
        "mercury setup model|tts|stt|terminal|gateway|observatory|tools|telemetry|agent|context|approvals|hermes-approvals|omp-approvals",
    )
    setup_parser.add_argument(
        "section",
        nargs="?",
        choices=[
            "model",
            "tts",
            "stt",
            "terminal",
            "gateway",
            "observatory",
            "tools",
            "telemetry",
            "agent",
            "context",
            "approvals",
            "hermes-approvals",
            "omp-approvals",
        ],
        default=None,
        help="Run a specific setup section instead of the full wizard",
    )
    setup_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Non-interactive mode (use defaults/env vars)",
    )
    setup_parser.add_argument(
        "--reset", action="store_true", help="Reset configuration to defaults"
    )
    setup_parser.add_argument(
        "--reconfigure",
        action="store_true",
        help="(Default on existing installs.) Re-run the full wizard, "
        "showing current values as defaults. Kept for backwards "
        "compatibility — a bare 'mercury setup' now does this.",
    )
    setup_parser.add_argument(
        "--quick",
        action="store_true",
        help="On existing installs: only prompt for items that are missing "
        "or unset, instead of running the full reconfigure wizard.",
    )
    setup_parser.add_argument(
        "--portal",
        action="store_true",
        help="One-shot Nous Portal setup: log in via OAuth, pick a Nous "
        "model, set Nous as the inference provider, and opt into the Tool "
        "Gateway. Skips the rest of the wizard.",
    )
    setup_parser.add_argument(
        "--install-sidecar",
        action="store_true",
        help="With 'observatory': provision, then install/enable/start the "
        "mercury-observatory.service sidecar unit (repair path)",
    )
    setup_parser.add_argument(
        "--no-encrypt-rooms",
        action="store_true",
        help="With 'observatory': create rooms WITHOUT end-to-end encryption "
        "(explicit plaintext opt-in — Matrix traffic including prompts is "
 "unencrypted on the tailnet; headless default stays encrypted)",
    )
    setup_parser.add_argument(
        "--stt-provider",
        default="",
        help="With 'stt': qwen3-asr | parakeet | openai (or MERCURY_STT_PROVIDER)",
    )
    setup_parser.add_argument(
        "--stt-model",
        default="",
        help="With 'stt': model/checkpoint label (or MERCURY_STT_MODEL)",
    )
    setup_parser.add_argument(
        "--stt-endpoint",
        default="",
        help="With 'stt': ASR server endpoint or API base URL (or MERCURY_STT_ENDPOINT)",
    )
    setup_parser.add_argument(
        "--stt-language",
        default="",
        help="With 'stt': language code, e.g. en (or MERCURY_STT_LANGUAGE)",
    )
    setup_parser.add_argument(
        "--mirc-url",
        default="",
        help="With 'stt': MIRC host base URL (or MERCURY_MIRC_URL)",
    )
    setup_parser.add_argument(
        "--mlounge-url",
        default="",
        help="With 'stt': mLounge host URL (or MERCURY_MLOUNGE_URL)",
    )
    setup_parser.add_argument(
        "--sidecar-url",
        default="",
        help="With 'stt': STT sidecar URL on the mLounge host (or MERCURY_STT_SIDECAR_URL)",
    )
    setup_parser.set_defaults(func=cmd_setup)
