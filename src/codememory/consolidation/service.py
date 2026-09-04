"""Deterministic candidate consolidation and card lifecycle policy."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Iterable

from ..storage.repository import MemoryRepository
from ..quality.models import QualityDecision
from ..quality.service import QualityService
from .models import CardStatus, ConsolidationAction, ConsolidationItem, ConsolidationResult
from .store import CardStore, _json, _unique_strings


POLICY_VERSION = "consolidator-v1"
_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", re.UNICODE)
_PREFIX_RE = re.compile(r"^(?:coding route observed|observed issue or rejected path|coding outcome)\s*:\s*", re.I)


@dataclass(frozen=True)
class _Match:
    card: sqlite3.Row
    version: sqlite3.Row
    score: float
    text_score: float
    binding_score: float


def _norm_text(value: Any) -> str:
    text = str(value or "").casefold().strip()
    text = _PREFIX_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _tokens(value: Any) -> set[str]:
    text = _norm_text(value)
    result: set[str] = set()
    for part in _WORD_RE.findall(text):
        if len(part) > 1 and all("\u4e00" <= ch <= "\u9fff" for ch in part):
            # Chinese phrases are represented by overlapping bigrams so a
            # short user query can still match a longer historical sentence.
            result.update(part[index : index + 2] for index in range(len(part) - 1))
            if len(part) >= 3:
                result.update(part[index : index + 3] for index in range(len(part) - 2))
        elif len(part) > 1:
            result.add(part)
    return result


def _similarity(left: Any, right: Any) -> float:
    a = _tokens(left)
    b = _tokens(right)
    if not a or not b:
        return 1.0 if _norm_text(left) == _norm_text(right) and _norm_text(left) else 0.0
    return len(a & b) / len(a | b)


def _binding_target(binding: dict[str, Any]) -> str:
    return CardStore.normalized_target(binding)


def _binding_targets(bindings: Iterable[dict[str, Any]]) -> set[str]:
    return {_binding_target(binding) for binding in bindings if isinstance(binding, dict) and _binding_target(binding)}


def _binding_overlap(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _canonical_key(candidate: dict[str, Any]) -> str:
    targets = sorted(_binding_targets(candidate.get("bindings") or []))
    text = _norm_text(candidate.get("statement"))
    # Keep the key compact and deterministic; the readable statement/aliases
    # remain in the version row and FTS projection.
    digest = hashlib.sha256(
        _json({"kind": candidate.get("kind"), "text": text, "targets": targets}).encode("utf-8")
    ).hexdigest()[:32]
    return f"{candidate.get('kind', 'unknown')}:{digest}"


def _merge_bindings(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for binding in [*old, *new]:
        if not isinstance(binding, dict):
            continue
        key = (str(binding.get("role") or "supporting_symbol"), _binding_target(binding))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        result.append(dict(binding))
    return result


class ConsolidationService:
    """Turn immutable candidate observations into auditable card versions."""

    def __init__(
        self,
        repository: MemoryRepository,
        *,
        store: CardStore | None = None,
        quality_service: QualityService | None = None,
        policy_version: str = POLICY_VERSION,
    ) -> None:
        self.repository = repository
        self.store = store or CardStore(repository.db)
        self.quality_service = quality_service or QualityService(repository)
        self.policy_version = policy_version

    def consolidate(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        candidate_ids: Iterable[str] | None = None,
        limit: int = 1000,
    ) -> ConsolidationResult:
        candidates = self.store.list_candidates(
            project_id=project_id,
            task_id=task_id,
            candidate_ids=candidate_ids,
            limit=limit,
        )
        items: list[ConsolidationItem] = []
        counts = {key: 0 for key in ("created", "merged", "new_versions", "uncertain", "skipped", "rejected")}
        for candidate in candidates:
            quality = self.quality_service.review_candidate(candidate)
            if quality.decision == QualityDecision.QUARANTINE.value:
                items.append(self._record_quarantined(candidate, quality))
                counts["rejected"] += 1
                continue
            item = self._consolidate_one(candidate)
            items.append(item)
            if item.action == ConsolidationAction.CREATE:
                counts["created"] += 1
            elif item.action == ConsolidationAction.MERGE:
                counts["merged"] += 1
            elif item.action == ConsolidationAction.NEW_VERSION:
                counts["new_versions"] += 1
            elif item.action == ConsolidationAction.UNCERTAIN:
                counts["uncertain"] += 1
            elif item.action == ConsolidationAction.REJECT:
                counts["rejected"] += 1
            else:
                counts["skipped"] += 1
        return ConsolidationResult(
            candidate_count=len(candidates),
            processed=sum(1 for item in items if item.action != ConsolidationAction.SKIP),
            created=counts["created"],
            merged=counts["merged"],
            new_versions=counts["new_versions"],
            uncertain=counts["uncertain"],
            skipped=counts["skipped"],
            rejected=counts["rejected"],
            items=tuple(items),
        )

    def _record_quarantined(
        self, candidate: dict[str, Any], quality: Any
    ) -> ConsolidationItem:
        """Persist a deterministic reject decision without touching cards."""

        decision_key = self.store.decision_key(candidate, self.policy_version)
        candidate_id = str(candidate["candidate_id"])
        with self.repository.db.transaction() as conn:
            previous = self.store.existing_decision(conn, decision_key)
            if previous is not None:
                return ConsolidationItem(
                    candidate_id=candidate_id,
                    action=ConsolidationAction.SKIP.value,
                    card_id=str(previous["card_id"]) if previous["card_id"] else None,
                    card_version_id=str(previous["card_version_id"])
                    if previous["card_version_id"]
                    else None,
                    status=None,
                    score=float(previous["score"]) if previous["score"] is not None else None,
                    reason="quarantine decision was already recorded",
                )
            reason = "quality quarantine: " + "; ".join(list(quality.reasons)[:5])
            self.store.record_decision(
                conn,
                decision_key=decision_key,
                project_id=str(candidate["project_id"]),
                task_id=str(candidate["task_id"]),
                candidate_id=candidate_id,
                action=ConsolidationAction.REJECT.value,
                card_id=None,
                card_version_id=None,
                score=float(quality.quality_score),
                reason=reason,
                metadata={"quality": quality.as_dict()},
            )
        return ConsolidationItem(
            candidate_id=candidate_id,
            action=ConsolidationAction.REJECT.value,
            card_id=None,
            card_version_id=None,
            status=None,
            score=float(quality.quality_score),
            reason=reason,
        )

    def _consolidate_one(self, candidate: dict[str, Any]) -> ConsolidationItem:
        decision_key = self.store.decision_key(candidate, self.policy_version)
        candidate_id = str(candidate["candidate_id"])
        with self.repository.db.transaction() as conn:
            previous = self.store.existing_decision(conn, decision_key)
            if previous is not None:
                card_id = str(previous["card_id"]) if previous["card_id"] else None
                version_id = str(previous["card_version_id"]) if previous["card_version_id"] else None
                status = None
                if card_id:
                    row = conn.execute("SELECT status FROM memory_cards WHERE card_id=?", (card_id,)).fetchone()
                    status = str(row["status"]) if row else None
                return ConsolidationItem(
                    candidate_id=candidate_id,
                    action=ConsolidationAction.SKIP.value,
                    card_id=card_id,
                    card_version_id=version_id,
                    status=status,
                    score=float(previous["score"]) if previous["score"] is not None else None,
                    reason="same candidate fingerprint and policy were already consolidated",
                )

            self._validate_candidate_evidence(conn, candidate)
            matches = self._find_matches(conn, candidate)
            if matches and self._is_ambiguous(matches):
                card_id, version_id = self._create_card(
                    conn,
                    candidate,
                    status=CardStatus.UNCERTAIN.value,
                    reason="multiple active cards matched within the ambiguity margin",
                )
                self._record_hints(conn, version_id, candidate)
                self.store.record_decision(
                    conn,
                    decision_key=decision_key,
                    project_id=str(candidate["project_id"]),
                    task_id=str(candidate["task_id"]),
                    candidate_id=candidate_id,
                    action=ConsolidationAction.UNCERTAIN.value,
                    card_id=card_id,
                    card_version_id=version_id,
                    score=matches[0].score,
                    reason="ambiguous match; created an explicitly uncertain card",
                    metadata={"matches": self._match_metadata(matches)},
                )
                return ConsolidationItem(
                    candidate_id=candidate_id,
                    action=ConsolidationAction.UNCERTAIN.value,
                    card_id=card_id,
                    card_version_id=version_id,
                    status=CardStatus.UNCERTAIN.value,
                    score=matches[0].score,
                    reason="ambiguous match; manual review required",
                )

            if not matches:
                status = CardStatus.PROPOSED.value
                card_id, version_id = self._create_card(
                    conn,
                    candidate,
                    status=status,
                    reason="first evidence-backed observation for this route/key",
                )
                self._record_hints(conn, version_id, candidate)
                action = ConsolidationAction.CREATE.value
                reason = "created a proposed card from an evidence-backed candidate"
                score = None
            else:
                match = matches[0]
                card_id = str(match.card["card_id"])
                version_id, action, reason = self._merge_or_version(conn, candidate, match)
                score = match.score
                self._record_hints(conn, version_id, candidate)

            self.store.record_decision(
                conn,
                decision_key=decision_key,
                project_id=str(candidate["project_id"]),
                task_id=str(candidate["task_id"]),
                candidate_id=candidate_id,
                action=action,
                card_id=card_id,
                card_version_id=version_id,
                score=score,
                reason=reason,
                metadata={"matches": self._match_metadata(matches)},
            )
            card_row = conn.execute("SELECT status FROM memory_cards WHERE card_id=?", (card_id,)).fetchone()
            return ConsolidationItem(
                candidate_id=candidate_id,
                action=action,
                card_id=card_id,
                card_version_id=version_id,
                status=str(card_row["status"]) if card_row else None,
                score=score,
                reason=reason,
            )

    @staticmethod
    def _validate_candidate_evidence(conn: sqlite3.Connection, candidate: dict[str, Any]) -> None:
        event_ids = [str(item) for item in candidate.get("evidence_event_ids") or [] if str(item)]
        if not event_ids:
            raise ValueError(f"candidate {candidate['candidate_id']} has no evidence")
        placeholders = ",".join("?" for _ in event_ids)
        rows = conn.execute(
            f"SELECT event_id,project_id,task_id FROM events WHERE event_id IN ({placeholders})",
            event_ids,
        ).fetchall()
        known = {str(row["event_id"]) for row in rows}
        missing = [event_id for event_id in event_ids if event_id not in known]
        if missing:
            raise ValueError(f"candidate {candidate['candidate_id']} references missing events: {missing[:5]}")
        mismatched = [
            str(row["event_id"])
            for row in rows
            if str(row["project_id"]) != str(candidate["project_id"])
            or str(row["task_id"]) != str(candidate["task_id"])
        ]
        if mismatched:
            raise ValueError(
                f"candidate {candidate['candidate_id']} references events outside its task/project: "
                f"{mismatched[:5]}"
            )

    def _find_matches(self, conn: sqlite3.Connection, candidate: dict[str, Any]) -> list[_Match]:
        candidate_text = " ".join(
            [str(candidate.get("statement") or ""), *[str(x) for x in candidate.get("aliases") or []]]
        )
        candidate_targets = _binding_targets(candidate.get("bindings") or [])
        rows = conn.execute(
            "SELECT c.*, v.card_version_id, v.version_no, v.statement, v.aliases_json, "
            "v.confidence AS version_confidence, v.uncertainty AS version_uncertainty "
            "FROM memory_cards c JOIN memory_card_versions v ON v.card_version_id=c.current_version_id "
            "WHERE c.project_id=? AND c.kind=? AND c.status NOT IN ('rejected','superseded')",
            (candidate["project_id"], candidate["kind"]),
        ).fetchall()
        # Fetch bindings in one query instead of doing an N+1 lookup for every
        # active card.  Large local imports commonly produce thousands of
        # cards; keeping the matching pass bounded is important for replay and
        # for an interactive consolidation command.
        targets_by_version: dict[str, set[str]] = {}
        version_ids = [str(row["card_version_id"]) for row in rows]
        if version_ids:
            placeholders = ",".join("?" for _ in version_ids)
            binding_rows = conn.execute(
                "SELECT card_version_id,normalized_target FROM memory_card_bindings "
                f"WHERE card_version_id IN ({placeholders})",
                version_ids,
            ).fetchall()
            for binding in binding_rows:
                target = str(binding["normalized_target"] or "")
                if target:
                    targets_by_version.setdefault(str(binding["card_version_id"]), set()).add(target)

        # A repeated coding route normally shares at least one path/symbol.
        # Restrict the expensive similarity pass to those rows when possible;
        # if no target intersects, retain the full set so a legitimate rename
        # can still be recognized by its natural-language statement.
        if candidate_targets:
            targeted_rows = [
                row
                for row in rows
                if candidate_targets & targets_by_version.get(str(row["card_version_id"]), set())
            ]
            if targeted_rows:
                rows = targeted_rows
        matches: list[_Match] = []
        for row in rows:
            card_targets = targets_by_version.get(str(row["card_version_id"]), set())
            card_text = " ".join([str(row["statement"] or ""), *[str(x) for x in (_json_load(row["aliases_json"], []))]])
            text_score = _similarity(candidate_text, card_text)
            binding_score = _binding_overlap(candidate_targets, card_targets)
            if candidate_targets and card_targets:
                score = (0.58 * text_score) + (0.42 * binding_score)
                # A repeated route often mentions one canonical entry file
                # while adding/removing supporting files.  An exact target
                # intersection is therefore stronger evidence than a strict
                # set-union ratio, provided the natural-language intent has
                # at least a modest overlap.
                if candidate_targets & card_targets and text_score >= 0.25:
                    score = max(score, 0.56 + min(0.30, text_score * 0.30))
            else:
                score = text_score
            if _norm_text(candidate.get("statement")) == _norm_text(row["statement"]):
                score = 1.0
            minimum = 0.56 if candidate_targets and card_targets else 0.72
            if score >= minimum:
                matches.append(_Match(row, row, min(1.0, score), text_score, binding_score))
        matches.sort(key=lambda item: (item.score, item.text_score, item.binding_score, str(item.card["card_id"])), reverse=True)
        return matches[:8]

    @staticmethod
    def _is_ambiguous(matches: list[_Match]) -> bool:
        return len(matches) > 1 and matches[0].score < 0.9 and (matches[0].score - matches[1].score) < 0.08

    @staticmethod
    def _match_metadata(matches: list[_Match]) -> list[dict[str, Any]]:
        return [
            {
                "card_id": str(item.card["card_id"]),
                "score": round(item.score, 4),
                "text_score": round(item.text_score, 4),
                "binding_score": round(item.binding_score, 4),
            }
            for item in matches
        ]

    def _create_card(
        self,
        conn: sqlite3.Connection,
        candidate: dict[str, Any],
        *,
        status: str,
        reason: str,
    ) -> tuple[str, str]:
        canonical_key = _canonical_key(candidate)
        card_id = self.store.new_card_id(str(candidate["project_id"]), str(candidate["kind"]), canonical_key)
        try:
            return self.store.insert_card(
                conn,
                card_id=card_id,
                project_id=str(candidate["project_id"]),
                task_id=str(candidate["task_id"]),
                kind=str(candidate["kind"]),
                canonical_key=canonical_key,
                status=status,
                confidence=float(candidate["confidence"]),
                statement=str(candidate["statement"]),
                aliases=[str(value) for value in candidate.get("aliases") or []],
                uncertainty=str(candidate["uncertainty"]),
                candidate_id=str(candidate["candidate_id"]),
                evidence_event_ids=[str(value) for value in candidate.get("evidence_event_ids") or []],
                bindings=list(candidate.get("bindings") or []),
                reason=reason,
            )
        except sqlite3.IntegrityError:
            # A deterministic key may already exist from a concurrent process;
            # surface the existing card rather than creating a random duplicate.
            row = conn.execute(
                "SELECT card_id,current_version_id FROM memory_cards WHERE project_id=? AND kind=? AND canonical_key=?",
                (candidate["project_id"], candidate["kind"], canonical_key),
            ).fetchone()
            if row is None:
                raise
            return str(row["card_id"]), str(row["current_version_id"])

    def _merge_or_version(
        self,
        conn: sqlite3.Connection,
        candidate: dict[str, Any],
        match: _Match,
    ) -> tuple[str, str, str]:
        card_id = str(match.card["card_id"])
        current_version_id = str(match.card["card_version_id"])
        current_aliases = _json_load(match.card["aliases_json"], [])
        candidate_aliases = [str(value) for value in candidate.get("aliases") or []]
        current_bindings = [
            {
                "role": str(binding["role"]),
                "path": binding["path"],
                "symbol": binding["symbol"],
                "qualified_symbol": binding["qualified_symbol"],
                "snapshot_id": binding["snapshot_id"],
                "evidence": _json_load(binding["evidence_event_ids_json"], []),
            }
            for binding in conn.execute(
                "SELECT * FROM memory_card_bindings WHERE card_version_id=?",
                (current_version_id,),
            ).fetchall()
        ]
        candidate_text = " ".join([str(candidate.get("statement") or ""), *candidate_aliases])
        current_text = " ".join([str(match.card["statement"] or ""), *[str(x) for x in current_aliases]])
        text_score = _similarity(candidate_text, current_text)
        old_targets = _binding_targets(current_bindings)
        new_targets = _binding_targets(candidate.get("bindings") or [])
        overlap = _binding_overlap(old_targets, new_targets)
        material_change = text_score < 0.62 or (old_targets and new_targets and overlap < 0.35)
        if not material_change:
            self.store.add_evidence(
                conn,
                current_version_id,
                str(candidate["candidate_id"]),
                [str(value) for value in candidate.get("evidence_event_ids") or []],
            )
            now = _now_for_store()
            conn.execute(
                "UPDATE memory_cards SET confidence=MAX(confidence,?), updated_at=? WHERE card_id=?",
                (float(candidate["confidence"]), now, card_id),
            )
            self.store.refresh_fts(conn, card_id)
            return current_version_id, ConsolidationAction.MERGE.value, "merged additional evidence into the current card version"

        aliases = _unique_strings([*current_aliases, *candidate_aliases])
        bindings = _merge_bindings(current_bindings, list(candidate.get("bindings") or []))
        version_id, _ = self.store.append_version(
            conn,
            card=match.card,
            current_version=match.version,
            statement=str(candidate["statement"]),
            aliases=aliases,
            confidence=max(float(match.card["confidence"]), float(candidate["confidence"])),
            uncertainty=str(candidate["uncertainty"]),
            candidate_id=str(candidate["candidate_id"]),
            evidence_event_ids=[str(value) for value in candidate.get("evidence_event_ids") or []],
            bindings=bindings,
            reason="new observation materially changed the statement or code binding",
        )
        # A material observation invalidates an unverified assumption.  Keep
        # the card queryable, but require review again when a previously
        # verified/stable route changes.  The old version and its evidence stay
        # immutable and are linked through ``supersedes``.
        old_status = str(match.card["status"])
        if old_status in {CardStatus.VERIFIED.value, CardStatus.STABLE.value}:
            now = _now_for_store()
            conn.execute(
                "UPDATE memory_cards SET status='uncertain', updated_at=? WHERE card_id=?",
                (now, card_id),
            )
            conn.execute(
                "INSERT INTO memory_card_lifecycle_events(lifecycle_event_id,card_id,from_status,to_status,actor,reason,metadata_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()),
                    card_id,
                    old_status,
                    CardStatus.UNCERTAIN.value,
                    "consolidator",
                    "material observation requires re-verification",
                    _json({"new_version_id": version_id, "candidate_id": candidate["candidate_id"]}),
                    now,
                ),
            )
        return version_id, ConsolidationAction.NEW_VERSION.value, "created a new version and linked it with supersedes"

    @staticmethod
    def _record_hints(conn: sqlite3.Connection, version_id: str, candidate: dict[str, Any]) -> None:
        # Relation hints are advisory.  They are persisted only after the
        # candidate and version have passed evidence validation.
        for hint in candidate.get("relation_hints") or []:
            if not isinstance(hint, dict):
                continue
            target_id = str(hint.get("target_id") or "").strip()
            target_type = str(hint.get("target_type") or "candidate").strip()
            relation = str(hint.get("relation") or "related_to").strip()
            if target_id and target_type in {"candidate", "event", "task", "file", "symbol", "card", "card_version"}:
                conn.execute(
                    "INSERT OR IGNORE INTO memory_card_links(card_version_id,target_type,target_id,relation_type,metadata_json,created_at) VALUES (?,?,?,?,?,?)",
                    (version_id, target_type, target_id, relation, _json(hint), _now_for_store()),
                )

    def transition_card(
        self,
        card_id: str,
        status: str,
        *,
        reason: str,
        actor: str = "operator",
    ) -> dict[str, Any]:
        return self.store.transition(card_id, status, reason=reason, actor=actor)

    def promote_card(self, card_id: str, *, reason: str, actor: str = "operator") -> dict[str, Any]:
        """Explicitly promote a proposed/verified card; never automatic."""

        card = self.store.get_card(card_id)
        if card is None:
            return {"updated": False, "card_id": card_id, "reason": "not_found"}
        target = CardStatus.STABLE.value if card["status"] in {"proposed", "verified", "uncertain"} else CardStatus.VERIFIED.value
        return self.store.transition(card_id, target, reason=reason, actor=actor)


def _json_load(raw: str | None, default: Any) -> Any:
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def _now_for_store() -> str:
    # Keep the helper local to avoid exposing a mutable clock API.  The store
    # uses the same second-resolution UTC format for all lifecycle writes.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
