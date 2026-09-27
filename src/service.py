from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_expected_version, require_iso_date,
                     require_materials, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    RENEWAL_ENTITY, RENEWAL_REVIEW_ROLES, RENEWAL_STATES,
                    RENEWAL_SUBMIT_ROLES, RENEWAL_VIEW_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, escalation_required, permit_expired,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_renewable, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        valid_from, valid_until = self._parse_term(payload, require_until=False)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor,
                                           valid_from, valid_until)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
            "permit_no": item["permit_no"],
            "valid_until": valid_until,
        })
        return self.enrich(item)

    @staticmethod
    def _parse_term(payload: Dict[str, Any], require_until: bool = True):
        valid_until = payload.get("valid_until")
        if require_until or valid_until is not None:
            valid_until = require_iso_date(valid_until, "valid_until")
        valid_from = payload.get("valid_from")
        if valid_from is not None:
            valid_from = require_iso_date(valid_from, "valid_from")
        if valid_from and valid_until and valid_from > valid_until:
            raise ValidationError("valid_from不能晚于valid_until")
        return valid_from, valid_until

    def submit_renewal(self, item_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_renewable(item["status"])
        open_renewal = self.repository.find_open_renewal(item_id)
        if open_renewal is not None:
            if open_renewal["status"] == "pending":
                raise ConflictError(
                    f"该许可已有待审续期申请{open_renewal['renewal_no']}，不能重复提交")
            raise ConflictError(
                f"申请{open_renewal['renewal_no']}已被退回，请按原编号补件再提交")
        valid_from, valid_until = self._parse_term(payload)
        materials = require_materials(payload.get("materials"))
        renewal = self.repository.create_renewal(
            item_id, item["permit_no"], valid_from, valid_until, materials, actor)
        self.repository.add_renewal_event(
            renewal["id"], "submit", actor,
            detail={"valid_from": valid_from, "valid_until": valid_until,
                    "materials": materials, "renewal_no": renewal["renewal_no"]})
        self.repository.append_audit("renewal_submit", RENEWAL_ENTITY, item_id, actor, {
            "renewal_id": renewal["id"], "renewal_no": renewal["renewal_no"],
            "permit_no": item["permit_no"], "valid_until": valid_until,
        })
        return self.renewal_detail(renewal["id"], role)

    def resubmit_renewal(self, renewal_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = require_expected_version(payload.get("expected_version"))
        renewal = self.repository.get_renewal(renewal_id)
        if renewal["status"] != "returned":
            raise ConflictError("仅被退回的申请可以补件再提交")
        item = self.repository.get_item(renewal["item_id"])
        validate_renewable(item["status"])
        valid_from, valid_until = self._parse_term(payload)
        materials = require_materials(payload.get("materials"))
        updated = self.repository.resubmit_renewal(
            renewal_id, valid_from, valid_until, materials, expected_version, actor)
        self.repository.add_renewal_event(
            renewal_id, "resubmit", actor,
            detail={"valid_from": valid_from, "valid_until": valid_until,
                    "materials": materials, "renewal_no": renewal["renewal_no"]})
        self.repository.append_audit("renewal_resubmit", RENEWAL_ENTITY,
                                     renewal["item_id"], actor, {
            "renewal_id": renewal_id, "renewal_no": renewal["renewal_no"],
            "valid_until": valid_until,
        })
        return self.renewal_detail(renewal_id, role)

    def review_renewal(self, renewal_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = require_expected_version(payload.get("expected_version"))
        decision = payload.get("decision")
        if decision not in ("approve", "return"):
            raise ValidationError("decision必须是approve或return")
        comment = payload.get("comment")
        if comment is not None:
            comment = require_text(comment, "comment", 2000)
        if decision == "return" and not comment:
            raise ValidationError("退回申请必须填写意见")
        renewal = self.repository.get_renewal(renewal_id)
        if renewal["status"] != "pending":
            raise ConflictError("该申请不在待审状态")
        item = self.repository.get_item(renewal["item_id"])
        if decision == "approve":
            blockers = []
            if not renewal["materials"]:
                blockers.append("续期材料缺失，不能批准")
            if item["status"] != "approved":
                blockers.append("许可未处于已批准状态，不能批准")
            if permit_expired(item["valid_until"]):
                blockers.append("原许可已过期，不能批准续期")
            if blockers:
                raise ConflictError("；".join(blockers))
            self.repository.approve_renewal(
                renewal_id, renewal["item_id"], renewal["requested_valid_from"],
                renewal["requested_valid_until"], expected_version, item["version"])
        else:
            self.repository.decide_renewal(renewal_id, "return", expected_version)
        self.repository.add_renewal_event(
            renewal_id, decision, actor, comment,
            detail={"renewal_no": renewal["renewal_no"]} if decision == "return"
            else {"renewal_no": renewal["renewal_no"],
                  "valid_from": renewal["requested_valid_from"],
                  "valid_until": renewal["requested_valid_until"]})
        self.repository.append_audit(
            "renewal_approve" if decision == "approve" else "renewal_return",
            RENEWAL_ENTITY, renewal["item_id"], actor, {
                "renewal_id": renewal_id, "renewal_no": renewal["renewal_no"],
                "comment": comment,
                "valid_until": renewal["requested_valid_until"]
                    if decision == "approve" else None,
            })
        return self.renewal_detail(renewal_id, role)

    def renewal_detail(self, renewal_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_VIEW_ROLES)
        renewal = self.repository.get_renewal(renewal_id)
        renewal["events"] = self.repository.list_renewal_events(renewal_id)
        renewal["permit"] = self.enrich(self.repository.get_item(renewal["item_id"]))
        return renewal

    def list_renewals(self, item_id: Optional[int], role: str) -> list:
        ensure_role(role, RENEWAL_VIEW_ROLES)
        renewals = self.repository.list_renewals(item_id)
        for renewal in renewals:
            permit = self.repository.get_item(renewal["item_id"])
            renewal["permit_expired"] = permit_expired(permit["valid_until"])
            renewal["permit_status"] = permit["status"]
        return renewals

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["expired"] = permit_expired(item.get("valid_until"))
        return result
