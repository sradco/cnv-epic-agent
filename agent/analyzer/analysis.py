"""Monitoring need assessment, coverage detection, and proposal generation."""

from __future__ import annotations

from typing import Any

from schemas.issue_doc import IssueDoc  # noqa: F401 — re-export


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        normalized = item.strip().lower()
        if not normalized:
            continue
        if normalized not in seen:
            seen.add(normalized)
            ordered.append(normalized)
    return ordered


def _match_terms(text: str, terms: list[str]) -> list[str]:
    return _unique([t for t in terms if t.lower() in text])


# ---------------------------------------------------------------------------
# Phase 1: Monitoring Need Assessment
# ---------------------------------------------------------------------------


def assess_monitoring_need(
    issues: list[IssueDoc],
    needed_terms: list[str],
    not_needed_terms: list[str],
    needed_threshold: int = 2,
    not_needed_threshold: int = -1,
) -> dict[str, Any]:
    needed_evidence: list[dict[str, Any]] = []
    not_needed_evidence: list[dict[str, Any]] = []

    for issue in issues:
        text = issue.full_text()
        n_matches = _match_terms(text, needed_terms)
        nn_matches = _match_terms(text, not_needed_terms)
        if n_matches:
            needed_evidence.append({"issue_key": issue.key, "matches": n_matches})
        if nn_matches:
            not_needed_evidence.append({"issue_key": issue.key, "matches": nn_matches})

    needed_score = sum(len(e["matches"]) for e in needed_evidence)
    not_needed_score = sum(len(e["matches"]) for e in not_needed_evidence)
    score = needed_score - not_needed_score

    if score >= needed_threshold:
        need_state = "needed"
    elif score <= not_needed_threshold:
        need_state = "not_needed"
    else:
        need_state = "uncertain"

    abs_score = abs(score)
    confidence = "high" if abs_score >= 4 else ("medium" if abs_score >= 2 else "low")

    return {
        "need_state": need_state,
        "confidence": confidence,
        "score": score,
        "needed_evidence": needed_evidence,
        "not_needed_evidence": not_needed_evidence,
    }


# ---------------------------------------------------------------------------
# Phase 2: Coverage Detection
# ---------------------------------------------------------------------------


def evaluate_coverage(
    issues: list[IssueDoc],
    keywords_by_category: dict[str, list[str]],
) -> dict[str, Any]:
    coverage: dict[str, Any] = {}
    for category, terms in keywords_by_category.items():
        matches: list[dict[str, Any]] = []
        for issue in issues:
            found = _match_terms(issue.full_text(), terms)
            if found:
                matches.append({"issue_key": issue.key, "matches": found})
        coverage[category] = {"present": bool(matches), "matches": matches}
    return coverage


# ---------------------------------------------------------------------------
# Proposal Generation
# ---------------------------------------------------------------------------


def detect_feature_types(
    issues: list[IssueDoc],
    feature_type_signals: dict[str, list[str]],
) -> list[str]:
    """Detect feature types from the epic and child issues.

    Uses the epic summary/description as primary evidence.
    Child issues contribute only via multi-word signals
    (e.g. "live migration") to avoid false positives from
    generic single words like "controller" or "operator"
    appearing in unrelated child contexts.
    """
    if not issues:
        return []

    epic_text = issues[0].full_text()
    child_text = "\n".join(issue.full_text() for issue in issues[1:])

    detected: list[str] = []
    for ft, signals in feature_type_signals.items():
        for s in signals:
            s_lower = s.lower()
            if s_lower in epic_text:
                detected.append(ft)
                break
            if " " in s_lower and s_lower in child_text:
                detected.append(ft)
                break
    return detected


_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "on", "for", "to", "is",
    "it", "by", "as", "at", "be", "we", "do", "if", "up", "so", "no",
    "not", "with", "from", "that", "this", "will", "can", "has", "was",
    "are", "all", "new", "add", "get", "set", "use", "via", "also",
    "should", "need", "must", "may", "based", "support", "feature",
    "epic", "story", "task", "cnv", "ocp", "openshift", "kubevirt",
    "implement", "ensure", "update", "create", "enable", "provide",
})


def extract_domain_keywords(issues: list[IssueDoc]) -> list[str]:
    """Extract domain-specific keywords from issue summaries.

    Focuses on technical terms from the **epic summary** (first issue)
    that identify the feature's domain (e.g. "multicast", "infiniband",
    "swap", "pause probe").  Child issue summaries only contribute
    multi-word phrases to avoid diluting with generic terms.
    """
    import re

    if not issues:
        return []

    epic_text = re.sub(r"[^a-z0-9\s\-]", " ", issues[0].summary.lower())
    child_text = " ".join(
        re.sub(r"[^a-z0-9\s\-]", " ", issue.summary.lower())
        for issue in issues[1:]
    )

    # Single words from epic summary only (5+ chars, not stopwords)
    epic_words = epic_text.split()
    singles = [
        w for w in epic_words
        if w not in _STOPWORDS and len(w) >= 5
    ]

    # Multi-word phrases from epic + children
    all_text = f"{epic_text} {child_text}"
    all_words = all_text.split()
    bigrams = [
        f"{all_words[i]} {all_words[i+1]}"
        for i in range(len(all_words) - 1)
        if all_words[i] not in _STOPWORDS and all_words[i+1] not in _STOPWORDS
        and len(all_words[i]) >= 3 and len(all_words[i+1]) >= 3
    ]

    seen: set[str] = set()
    result: list[str] = []
    for phrase in bigrams + singles:
        if phrase not in seen:
            seen.add(phrase)
            result.append(phrase)

    return result


def propose_for_categories(
    missing_categories: list[str],
    feature_types: list[str],
    inventory: Any = None,
    issues: list[IssueDoc] | None = None,
    patterns_cfg: dict[str, Any] | None = None,
    all_coverage_categories: list[str] | None = None,
) -> dict[str, dict[str, list[dict[str, str]]]]:
    """Propose observability items for each missing category.

    Returns a two-section structure per category::

        {"metrics": {"existing": [...], "proposed": [...]}, ...}

    ``existing`` — artifacts found in the inventory that are relevant,
    with a rationale explaining *why* they matter for this epic.

    ``proposed`` — new instrumentation suggestions derived from
    observability patterns, with rationale and user-action guidance.

    Alert and dashboard proposals are only kept when backing metrics
    exist — either in the inventory or as proposed new metrics.  This
    prevents generating ungrounded alerts/dashboards that reference
    metrics no one has implemented.
    """
    proposals: dict[str, dict[str, list[dict[str, str]]]] = {}

    domain_keywords = extract_domain_keywords(issues) if issues else []
    epic_summary = issues[0].summary if issues else ""

    for category in missing_categories:
        existing = _propose_from_inventory(
            category, domain_keywords, inventory,
        ) if inventory and domain_keywords else []

        proposed = _propose_new_items(
            category, domain_keywords, feature_types,
            epic_summary, patterns_cfg or {},
        )

        proposals[category] = {"existing": existing, "proposed": proposed}

    has_metrics = _has_metric_backing(
        proposals, inventory, domain_keywords,
        all_coverage_categories or [],
    )
    if not has_metrics:
        for cat in ("alerts", "dashboards"):
            if cat in proposals:
                proposals[cat]["proposed"] = []

    return proposals


def _has_metric_backing(
    proposals: dict[str, dict[str, list[dict[str, str]]]],
    inventory: Any,
    domain_keywords: list[str],
    all_coverage_categories: list[str],
) -> bool:
    """Check if there are relevant metrics to back alerts/dashboards.

    Returns True when *any* of the following hold:
    - The epic already has metrics coverage (metrics not in gaps).
    - The proposals include new metric proposals.
    - The inventory contains domain-relevant existing metrics.
    """
    if "metrics" not in [
        cat for cat, data in proposals.items()
        if cat == "metrics"
    ]:
        if "metrics" in all_coverage_categories:
            return True

    metric_data = proposals.get("metrics", {})
    if metric_data.get("proposed"):
        return True
    if metric_data.get("existing"):
        return True

    if inventory and hasattr(inventory, "metrics") and domain_keywords:
        for m in inventory.metrics:
            combined = f"{m.name} {m.help}".lower()
            if any(kw in combined for kw in domain_keywords):
                return True

    return False


def _matched_keyword(name: str, extra: str, keywords: list[str]) -> str:
    """Return the first keyword that matches, or empty string."""
    combined = f"{name} {extra}".lower()
    for kw in keywords:
        if kw in combined:
            return kw
    return ""


def _propose_from_inventory(
    category: str,
    keywords: list[str],
    inventory: Any,
) -> list[dict[str, str]]:
    """Find relevant existing artifacts with a rationale for each."""
    items: list[dict[str, str]] = []
    seen: set[str] = set()

    if category == "metrics" and hasattr(inventory, "metrics"):
        for m in inventory.metrics:
            kw = _matched_keyword(m.name, m.help, keywords)
            if kw and m.name.lower() not in seen:
                seen.add(m.name.lower())
                items.append({
                    "name": m.name,
                    "type": m.metric_type,
                    "repo": m.repo,
                    "rationale": (
                        f"Already tracks {kw}-related behavior — "
                        f"verify it captures the new functionality"
                    ),
                })
    elif category == "alerts" and hasattr(inventory, "alerts"):
        for a in inventory.alerts:
            kw = _matched_keyword(a.name, a.expr, keywords)
            if kw and a.name.lower() not in seen:
                seen.add(a.name.lower())
                items.append({
                    "name": a.name,
                    "severity": a.severity,
                    "repo": a.repo,
                    "rationale": (
                        f"Existing alert covering {kw} failures — "
                        f"verify it triggers for the new code path"
                    ),
                })
    elif category == "dashboards" and hasattr(inventory, "panels"):
        for p in inventory.panels:
            kw = _matched_keyword(p.name, p.dashboard, keywords)
            if kw and p.name.lower() not in seen:
                seen.add(p.name.lower())
                items.append({
                    "name": p.name,
                    "dashboard": p.dashboard,
                    "repo": p.repo,
                    "rationale": (
                        f"Panel visualizing {kw} data — "
                        f"check if new functionality is reflected here"
                    ),
                })

    return items


def _propose_new_items(
    category: str,
    domain_keywords: list[str],
    feature_types: list[str],
    epic_summary: str,
    patterns_cfg: dict[str, Any],
) -> list[dict[str, str]]:
    """Generate new instrumentation proposals from observability patterns.

    Matches the epic's domain keywords and feature types against the
    ``observability_patterns`` config to produce concrete suggestions
    with rationale and user-action guidance.
    """
    proposals: list[dict[str, str]] = []
    seen_hints: set[str] = set()

    domain_label = _derive_domain_label(domain_keywords, epic_summary)

    for pattern_name, pattern in patterns_cfg.items():
        match_terms = [t.lower() for t in pattern.get("match_terms", [])]
        epic_lower = epic_summary.lower()
        matched = any(t in epic_lower for t in match_terms)
        if not matched:
            matched = any(
                ft.replace("_", " ") in pattern_name or pattern_name in ft
                for ft in feature_types
            )
        if not matched:
            continue

        cat_key = category
        if category == "dashboards":
            cat_key = "dashboards"
        templates = pattern.get(cat_key, [])

        for tmpl in templates:
            hint_key = "name_hint" if category != "dashboards" else "panel_hint"
            raw_hint = tmpl.get(hint_key, "")
            if not raw_hint:
                continue

            is_identifier = hint_key == "name_hint"
            hint = _fill_domain(
                raw_hint, domain_label, snake_case=is_identifier,
            )
            if hint.lower() in seen_hints:
                continue
            seen_hints.add(hint.lower())

            rationale = _fill_domain(
                tmpl.get("rationale", ""), domain_label,
            )
            user_action = _fill_domain(
                tmpl.get("user_action", ""), domain_label,
            )

            entry: dict[str, str] = {
                hint_key: hint,
                "rationale": rationale,
                "user_action": user_action,
            }
            if "type" in tmpl:
                entry["type"] = tmpl["type"]

            proposals.append(entry)

    return proposals


def _derive_domain_label(keywords: list[str], epic_summary: str) -> str:
    """Pick a short human-readable domain label from keywords or summary.

    Prefers multi-word keywords (bigrams) over single words, and falls
    back to the first 3 meaningful words of the epic summary.
    """
    bigrams = [kw for kw in keywords if " " in kw]
    if bigrams:
        return bigrams[0]
    if keywords:
        return keywords[0]

    words = [
        w for w in epic_summary.lower().split()
        if w not in _STOPWORDS and len(w) >= 4
    ]
    return " ".join(words[:3]) if words else "feature"


def _fill_domain(
    template: str,
    domain_label: str,
    snake_case: bool = False,
) -> str:
    """Substitute {domain}, {Domain}, {DomainCamel} in a template.

    When *snake_case* is True, spaces in the domain label are replaced
    with underscores for ``{domain}`` (metric/alert name contexts).
    ``{Domain}`` and ``{DomainCamel}`` are always space-free by nature.
    """
    domain_sub = domain_label.replace(" ", "_") if snake_case else domain_label
    result = template.replace("{domain}", domain_sub)
    result = result.replace("{Domain}", domain_label.title())
    camel = "".join(w.capitalize() for w in domain_label.split())
    result = result.replace("{DomainCamel}", camel)
    return result


def suggest_dashboard_targets(
    feature_types: list[str],
    dashboards: list[Any],
) -> list[str]:
    """Suggest which existing dashboards new panels should be added to.

    Uses the live inventory of discovered dashboards rather than a static
    config mapping. Matches dashboard names against feature-type keywords
    to find relevant targets.
    """
    _FEATURE_KEYWORDS: dict[str, list[str]] = {
        "api_controller": ["overview", "cluster", "operator"],
        "data_path": ["virtual machine", "vm ", "migration", "storage"],
        "performance_scale": ["utilization", "performance", "resource"],
        "availability_reliability": ["overview", "cluster", "health"],
    }

    targets: list[str] = []
    seen: set[str] = set()

    for ft in feature_types:
        keywords = _FEATURE_KEYWORDS.get(ft, [])
        for d in dashboards:
            name = d.name if hasattr(d, "name") else d.get("name", "")
            name_lower = name.lower()
            if name in seen:
                continue
            if any(kw in name_lower for kw in keywords):
                seen.add(name)
                repo = d.repo if hasattr(d, "repo") else d.get("repo", "")
                dtype = d.dashboard_type if hasattr(d, "dashboard_type") else d.get("type", "")
                targets.append(f"{name} ({repo}, {dtype})")

    return targets


def suggest_telemetry(
    inventory: Any = None,
    issues: list[IssueDoc] | None = None,
) -> list[dict[str, str]]:
    """Find cluster-level recording rules not yet on the CMO allowlist.

    Uses ``find_cluster_level_rules`` from ``discover`` to identify candidates
    and filters out those already present on the discovered CMO allowlist.

    When ``issues`` are provided, results are filtered to those relevant
    to the epic's domain keywords.  Without issues, all candidates are
    returned (useful for the standalone ``suggest_telemetry`` MCP tool).

    Each candidate includes a ``rationale`` explaining why it is suitable
    for telemetry collection.
    """
    if inventory is None:
        return []

    from mcpserver.github.discover import find_cluster_level_rules

    allowlist_names: set[str] = set()
    if hasattr(inventory, "telemetry_allowlist"):
        allowlist_names = {t.metric_name for t in inventory.telemetry_allowlist}

    domain_keywords = extract_domain_keywords(issues) if issues else []

    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    for r in find_cluster_level_rules(inventory):
        if r.name in seen or r.name in allowlist_names:
            continue

        if domain_keywords:
            combined = f"{r.name} {r.expr}".lower()
            if not any(kw in combined for kw in domain_keywords):
                continue

        seen.add(r.name)
        matched_kw = ""
        if domain_keywords:
            combined = f"{r.name} {r.expr}".lower()
            matched_kw = next(
                (kw for kw in domain_keywords if kw in combined), "",
            )

        rationale = _build_telemetry_rationale(r.name, r.expr, matched_kw)

        candidates.append({
            "name": r.name,
            "expr": r.expr,
            "repo": r.repo,
            "file": r.file,
            "rationale": rationale,
        })

    return candidates


def _build_telemetry_rationale(
    name: str, expr: str, matched_keyword: str,
) -> str:
    """Generate a rationale for why a recording rule is a telemetry candidate."""
    parts: list[str] = []

    parts.append(
        "Cluster-level recording rule not currently on the CMO telemetry allowlist."
    )

    if ":sum" in name:
        parts.append(
            "Aggregates to a cluster-wide sum — useful for adoption tracking."
        )
    elif ":count" in name:
        parts.append(
            "Aggregates to a cluster-wide count — useful for adoption and usage volume."
        )
    elif ":avg" in name:
        parts.append(
            "Aggregates to a cluster-wide average — useful for performance baseline tracking."
        )
    elif name.startswith("cluster:"):
        parts.append(
            "Explicitly scoped to cluster level — designed for fleet-wide visibility."
        )

    if matched_keyword:
        parts.append(
            f"Relevant to this epic's {matched_keyword} domain."
        )

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Full Analysis Pipeline
# ---------------------------------------------------------------------------


def build_analysis_result(
    epic: IssueDoc,
    related_issues: list[IssueDoc],
    cfg: dict[str, Any],
    inventory: Any = None,
) -> dict[str, Any]:
    analysis_cfg = cfg.get("analysis", {})
    need_cfg = analysis_cfg.get("need_assessment", {})
    coverage_cfg = analysis_cfg.get("coverage_keywords", {})
    proposal_cfg = cfg.get("proposals", {})

    issue_set = [epic] + related_issues

    need = assess_monitoring_need(
        issue_set,
        needed_terms=need_cfg.get("needed_terms", []),
        not_needed_terms=need_cfg.get("not_needed_terms", []),
        needed_threshold=int(need_cfg.get("needed_threshold", 2)),
        not_needed_threshold=int(need_cfg.get("not_needed_threshold", -1)),
    )

    coverage = evaluate_coverage(issue_set, coverage_cfg)

    missing = [cat for cat, data in coverage.items() if not data.get("present")]

    feature_types = detect_feature_types(
        issue_set, proposal_cfg.get("feature_type_signals", {})
    )

    patterns_cfg = cfg.get("observability_patterns", {})

    covered_categories = [
        cat for cat, data in coverage.items() if data.get("present")
    ]

    proposals = propose_for_categories(
        missing_categories=missing,
        feature_types=feature_types,
        inventory=inventory,
        issues=issue_set,
        patterns_cfg=patterns_cfg,
        all_coverage_categories=covered_categories,
    )

    discovered_dashboards = []
    if inventory is not None and hasattr(inventory, "dashboards"):
        discovered_dashboards = inventory.dashboards

    dashboard_target_list = suggest_dashboard_targets(
        feature_types, discovered_dashboards,
    )

    telemetry_suggestions = suggest_telemetry(inventory=inventory, issues=issue_set)

    if telemetry_suggestions and need["need_state"] == "needed":
        if "telemetry" not in missing:
            missing.append("telemetry")
        proposals["telemetry"] = {
            "existing": [],
            "proposed": [
                {
                    "name_hint": s["name"],
                    "expr": s["expr"],
                    "repo": s.get("repo", ""),
                    "rationale": s.get("rationale", ""),
                    "user_action": (
                        "Add match entry to CMO metrics.yaml allowlist "
                        "and verify in RHOBS staging."
                    ),
                }
                for s in telemetry_suggestions
            ],
        }

    if need["need_state"] == "needed" and missing:
        recommended_action = (
            "create now" if need["confidence"] == "high" else "review first"
        )
    elif need["need_state"] == "uncertain":
        recommended_action = "review first"
    else:
        recommended_action = "skip"

    apply_allowed = need["need_state"] == "needed" and bool(missing)

    domain_keywords = extract_domain_keywords(issue_set)

    child_issues_data = [
        {"key": c.key, "summary": c.summary, "description": c.description}
        for c in related_issues
    ]

    return {
        "epic_key": epic.key,
        "epic_summary": epic.summary,
        "epic_description": epic.description,
        "epic_labels": epic.labels or [],
        "child_issues": child_issues_data,
        "domain_keywords": domain_keywords,
        "need_state": need["need_state"],
        "need_confidence": need["confidence"],
        "need_score": need["score"],
        "need_evidence": {
            "needed": need["needed_evidence"],
            "not_needed": need["not_needed_evidence"],
        },
        "coverage": coverage,
        "gaps": missing if need["need_state"] == "needed" else [],
        "feature_types": feature_types,
        "proposals": proposals,
        "dashboard_targets": dashboard_target_list,
        "telemetry_suggestions": telemetry_suggestions,
        "recommended_action": recommended_action,
        "apply_allowed": apply_allowed,
        "would_create_count": len(missing) if apply_allowed else 0,
    }
