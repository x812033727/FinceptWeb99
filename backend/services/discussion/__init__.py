"""Discussion subsystem — context assembly + helpers.

`services.discussion_service` is still the top-level orchestrator
(persona resolution, round streaming, conclusion synthesis). This
subpackage hosts the modular pieces it composes:

  - `context.build_market_context` — assemble the market snapshot
    fed to every persona (split into per-area blocks for clarity
    and per-block testability).
"""
