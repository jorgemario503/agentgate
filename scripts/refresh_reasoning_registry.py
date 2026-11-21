"""Refresh the reasoning registry from a provider's model catalog.

The registry (core/reasoning/registry.py) is a hand-curated map of
provider -> model-pattern -> ReasoningStyle. It drifts whenever a provider
ships a model nobody has classified yet. This tool rebuilds the *generated*
part of that map from the provider's own catalog, where reasoning support is
published as machine-readable tags.

Source of truth
---------------
DeepInfra's OpenAI-compatible catalog reports, per model, a ``metadata.tags``
list. Two tags matter and map directly onto a ReasoningStyle:

    reasoning_effort   the model accepts a top-level ``reasoning_effort``
                       parameter  -> style "reasoning_effort"
    reasoning (alone)  reasoning-capable but no depth parameter to send
                       -> style "model_variant"

Anything else, or a model with neither tag, is not registered.

Why a tool and not a live lookup
--------------------------------
``resolve()`` runs on the request hot path; a network call there would add a
latency and failure mode to every reasoning request, and would make the
"unknown -> warn, do not silently send" behaviour unauditable. So the catalog
is consulted *deliberately* by this script, whose output is reviewed like any
other code change.

Usage
-----
    python scripts/refresh_reasoning_registry.py            # show the diff
    python scripts/refresh_reasoning_registry.py --apply    # write the fragment

``--apply`` writes core/reasoning/registry_generated.py, which registry.py
imports and layers under its hand-written patterns.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running as `python scripts/refresh_reasoning_registry.py` from the repo
# root without installing the package: put the repo root on sys.path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


#: Provider whose catalog we can read. Kept as a mapping so a second provider
#: can be added in one place once its catalog exposes equivalent tags.
CATALOG_ENDPOINTS: dict[str, str] = {
    "deepinfra": "https://api.deepinfra.com/v1/openai/models",
}

#: Tag -> registry style. Order matters only for documentation; a model with
#: both tags is reasoning_effort (the stronger, parameterised form).
TAG_TO_STYLE: dict[str, str] = {
    "reasoning_effort": "reasoning_effort",
    "reasoning": "model_variant",
}


def classify_tags(tags: list[str]) -> str | None:
    """Map a model's catalog tags to a ReasoningStyle, or None to skip.

    ``reasoning_effort`` wins over ``reasoning`` because a model carrying both
    supports the parameter; ``reasoning`` alone means capable-but-not-tunable.
    """
    tagset = {t.strip().lower() for t in (tags or [])}
    if "reasoning_effort" in tagset:
        return TAG_TO_STYLE["reasoning_effort"]
    if "reasoning" in tagset:
        return TAG_TO_STYLE["reasoning"]
    return None


def build_entries(models: list[dict]) -> list[tuple[str, str]]:
    """Turn a catalog listing into sorted (fnmatch-pattern, style) pairs.

    The pattern is the exact model id. Registry matching is fnmatch, and an
    exact id is its own most specific pattern, so a generated entry never
    shadows or is shadowed unexpectedly. Sorted for a stable, reviewable diff.
    """
    out: list[tuple[str, str]] = []
    for m in models:
        model_id = m.get("id")
        if not model_id:
            continue
        meta = m.get("metadata") or {}
        style = classify_tags(meta.get("tags") or [])
        if style is None:
            continue
        out.append((model_id, style))
    return sorted(out)


def current_generated() -> list[tuple[str, str]]:
    """The patterns/styles currently shipped in the generated fragment.

    Imported lazily so the script runs even if the fragment is absent yet.
    """
    try:
        from core.reasoning.registry_generated import DEEPINFRA_GENERATED
    except Exception:
        return []
    return sorted((p, e.style) for p, e in DEEPINFRA_GENERATED)


def diff(
    old: list[tuple[str, str]], new: list[tuple[str, str]]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str, str]]]:
    """(added, removed, changed) between current and fetched entries."""
    old_map = dict(old)
    new_map = dict(new)
    added = sorted((p, s) for p, s in new_map.items() if p not in old_map)
    removed = sorted((p, s) for p, s in old_map.items() if p not in new_map)
    changed = sorted(
        (p, old_map[p], new_map[p])
        for p in new_map
        if p in old_map and old_map[p] != new_map[p]
    )
    return added, removed, changed


def fetch_catalog(provider: str, api_key: str, *, timeout: float = 30.0) -> list[dict]:
    """GET the provider catalog. Network lives only here, behind one call."""
    import httpx

    url = CATALOG_ENDPOINTS[provider]
    resp = httpx.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        return data.get("data", [])
    return data if isinstance(data, list) else []


def render_fragment(provider: str, entries: list[tuple[str, str]], source: str) -> str:
    """Python source for the generated fragment module."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        '"""GENERATED — do not edit by hand.',
        "",
        f"Produced by scripts/refresh_reasoning_registry.py on {stamp}",
        f"from {source}",
        "",
        "Regenerate with:",
        "    python scripts/refresh_reasoning_registry.py --apply",        '"""',
        "from __future__ import annotations",
        "",
        "from core.reasoning.registry import ModelReasoning",
        "",
        "#: (fnmatch pattern, ModelReasoning) for models whose reasoning support",
        "#: is published in the provider catalog. Layered *under* the hand-written",
        "#: patterns in registry.py, so an explicit human entry still wins.",
        f"{provider.upper()}_GENERATED: list[tuple[str, ModelReasoning]] = [",
    ]
    for pattern, style in entries:
        lines.append(f'    ("{pattern}", ModelReasoning("{style}")),')
    lines.append("]")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider", default="deepinfra", choices=sorted(CATALOG_ENDPOINTS))
    parser.add_argument(
        "--apply", action="store_true",
        help="write core/reasoning/registry_generated.py instead of only printing the diff",
    )
    args = parser.parse_args(argv)

    # Resolve the key through the same settings layer the runners use, so the
    # tool honours .env / AGENTGATE_SET exactly as a bot would. Importing
    # core.providers loads core.config, which triggers the dotenv load.
    from core.providers import settings_for

    api_key = settings_for(args.provider).api_key
    if not api_key:
        print(f"error: {args.provider.upper()}_API_KEY is not set", file=sys.stderr)
        return 2

    try:
        models = fetch_catalog(args.provider, api_key)
    except Exception as e:  # network / auth failure: report, do not traceback
        print(f"error: could not fetch {args.provider} catalog: {e}", file=sys.stderr)
        return 1

    new_entries = build_entries(models)
    old_entries = current_generated()
    added, removed, changed = diff(old_entries, new_entries)

    print(f"{args.provider}: {len(models)} models in catalog, "
          f"{len(new_entries)} reasoning-capable, {len(old_entries)} currently generated")
    for pattern, style in added:
        print(f"  + {pattern}  ({style})")
    for pattern, style in removed:
        print(f"  - {pattern}  (was {style})")
    for pattern, old_style, new_style in changed:
        print(f"  ~ {pattern}  {old_style} -> {new_style}")

    if not (added or removed or changed):
        print("no changes.")
        return 0

    if not args.apply:
        print("\n(dry run — re-run with --apply to write the fragment)")
        return 0

    out_path = Path(__file__).resolve().parent.parent / "core" / "reasoning" / "registry_generated.py"
    source = CATALOG_ENDPOINTS[args.provider]
    out_path.write_text(render_fragment(args.provider, new_entries, source))
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
