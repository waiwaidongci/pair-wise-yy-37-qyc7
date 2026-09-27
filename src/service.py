from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_materials, require_number,
                     require_period, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES,
                    RENEWAL_ENTITY, RENEWAL_REVIEW_ROLES,
                    RENEWAL_SUBMIT_ROLES, RENEWAL_VIEW_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, renewal_blockers, response_deadline_hours,
                    role_for_transition, validate_transition)


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
        valid_from = valid_until = None
        if payload.get("valid_from") is not None or payload.get("valid_until") is not None:
            valid_from, valid_until = require_period(
                payload.get("valid_from"), payload.get("valid_until"))
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor,
                                           valid_from, valid_until)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

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

    # ----- 续期办理 -----
    def _load_approved_permit(self, item_id: int) -> Dict[str, Any]:
        item = self._permit(item_id)
        if item["status"] != "approved":
            raise ConflictError("只有已批准的排污许可才能办理续期")
        return item

    def _permit(self, item_id: int) -> Dict[str, Any]:
        return self.repository.get_item(item_id)

    def _renewal_detail(self, renewal: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(renewal)
        result["materials"] = self.repository.list_renewal_materials(renewal["id"])
        result["reviews"] = self.repository.list_renewal_reviews(renewal["id"])
        return result

    def submit_renewal(self, item_id: int, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        """申请人按原许可号和当前版本提交续期；同一许可同时仅一份待审申请。"""
        ensure_role(role, RENEWAL_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self._load_approved_permit(item_id)
        permit_no = payload.get("permit_no")
        permit_no = require_text(permit_no, "permit_no", 100) if permit_no is not None else None
        if item["external_ref"] and permit_no != item["external_ref"]:
            raise ValidationError("permit_no与原许可号不一致")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError("expected_version必须是正整数")
        if expected_version != item["version"]:
            raise ConflictError("版本冲突，请按当前版本提交续期")
        valid_from, valid_until = require_period(
            payload.get("valid_from"), payload.get("valid_until"),
            require_future_end=True)
        materials = require_materials(payload.get("materials"))
        active = self.repository.get_active_renewal(item_id)
        if active is not None:
            raise ConflictError(
                f"该许可已有待审续期申请{active['renewal_no']}，重复提交沿用原编号")
        renewal = self.repository.create_renewal(
            item_id, expected_version, valid_from, valid_until, materials, actor)
        self.repository.append_audit("renewal_submit", RENEWAL_ENTITY, item_id, actor, {
            "renewal_id": renewal["id"], "renewal_no": renewal["renewal_no"],
            "permit_no": item["external_ref"], "base_version": expected_version,
            "valid_from": valid_from, "valid_until": valid_until,
            "materials": len(materials),
        })
        return self._renewal_detail(renewal)

    def supplement_renewal(self, item_id: int, renewal_no: str,
                           payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        """退回后补件再提交，沿用原编号；新期限与新材料均可补充。"""
        ensure_role(role, RENEWAL_SUBMIT_ROLES)
        actor = require_text(actor, "actor", 100)
        renewal = self.repository.get_renewal_by_no(renewal_no)
        if renewal["item_id"] != item_id:
            raise NotFoundError("续期申请不存在")
        valid_from = payload.get("valid_from", renewal["valid_from"])
        valid_until = payload.get("valid_until", renewal["valid_until"])
        valid_from, valid_until = require_period(
            valid_from, valid_until, require_future_end=True)
        materials = payload.get("materials")
        if materials is not None:
            materials = require_materials(materials)
        else:
            materials = []
        if valid_from == renewal["valid_from"] and valid_until == renewal["valid_until"] \
                and not materials:
            raise ValidationError("请补充材料或更新期限后再提交")
        updated = self.repository.resubmit_renewal(
            renewal["id"], valid_from, valid_until, materials, actor)
        self.repository.append_audit("renewal_resubmit", RENEWAL_ENTITY, item_id, actor, {
            "renewal_id": renewal["id"], "renewal_no": renewal_no,
            "valid_from": valid_from, "valid_until": valid_until,
            "added_materials": len(materials),
        })
        return self._renewal_detail(updated)

    def review_renewal(self, item_id: int, renewal_no: str,
                       payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        """合规负责人核对材料：批准或退回，历次意见全部留存在详情。"""
        ensure_role(role, RENEWAL_REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        decision = payload.get("decision")
        if decision not in ("approve", "return"):
            raise ValidationError("decision必须是approve或return")
        opinion = payload.get("opinion")
        if opinion is not None:
            opinion = require_text(opinion, "opinion")
        if decision == "return" and not (opinion and opinion.strip()):
            raise ValidationError("退回续期必须填写整改意见")
        if opinion is None:
            opinion = "材料齐备，准予续期" if decision == "approve" else ""
        renewal = self.repository.get_renewal_by_no(renewal_no)
        if renewal["item_id"] != item_id:
            raise NotFoundError("续期申请不存在")
        if renewal["status"] != "submitted":
            raise ConflictError("只有待审续期申请可以审批")
        if decision == "return":
            result = self.repository.return_renewal(renewal["id"], opinion, actor)
            self.repository.append_audit("renewal_return", RENEWAL_ENTITY, item_id, actor, {
                "renewal_id": renewal["id"], "renewal_no": renewal_no,
                "opinion": opinion,
            })
            return self._renewal_detail(result)
        item = self._permit(item_id)
        blockers = renewal_blockers(
            self.repository.list_renewal_materials(renewal["id"]),
            item["valid_until"])
        if blockers:
            raise ConflictError("；".join(blockers))
        updated_item = self.repository.approve_renewal(renewal["id"], opinion, actor)
        self.repository.append_audit("renewal_approve", RENEWAL_ENTITY, item_id, actor, {
            "renewal_id": renewal["id"], "renewal_no": renewal_no,
            "permit_no": item["external_ref"], "opinion": opinion,
            "new_valid_from": renewal["valid_from"],
            "new_valid_until": renewal["valid_until"],
            "permit_version": updated_item["version"],
        })
        detail = self._renewal_detail(self.repository.get_renewal(renewal["id"]))
        detail["permit"] = self.enrich(updated_item)
        return detail

    def get_renewal(self, item_id: int, renewal_no: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RENEWAL_VIEW_ROLES)
        if not isinstance(item_id, int):
            raise ValidationError("item_id必须是整数")
        self._permit(item_id)
        renewal = self.repository.get_renewal_by_no(renewal_no)
        if renewal["item_id"] != item_id:
            raise NotFoundError("续期申请不存在")
        return self._renewal_detail(renewal)

    def list_renewals(self, item_id: Optional[int], role: str) -> list:
        ensure_role(role, RENEWAL_VIEW_ROLES)
        if item_id is not None:
            self._permit(item_id)
        return [self._renewal_detail(r)
                for r in self.repository.list_renewals(item_id)]

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
