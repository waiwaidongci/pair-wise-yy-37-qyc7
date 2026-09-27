import tempfile, unittest
from datetime import date, timedelta
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


def _days(offset):
    return (date.today() + timedelta(days=offset)).isoformat()


class RenewalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _approved_item(self, valid_until=None, external_ref="PERMIT-1"):
        item = self.service.create_item({
            "title": "permit", "description": "approved permit",
            "severity": "medium", "quantity": 3, "threshold": 5,
            "external_ref": external_ref,
            "valid_from": _days(-300), "valid_until": valid_until or _days(30),
        }, "creator", "applicant")
        current = item
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "reviewer",
                TRANSITION_ROLES[target][0])
        return current

    def _submit(self, item_id, **overrides):
        payload = {"valid_from": _days(30), "valid_until": _days(395),
                   "materials": ["排放监测报告", "治理设施运行记录"]}
        payload.update(overrides)
        return self.service.submit_renewal(item_id, payload, "applicant-user", "applicant")

    def test_submit_approve_writes_new_term_and_keeps_history(self):
        item = self._approved_item()
        self.service.add_record(item["id"], {
            "kind": "inspection", "detail": "现场检查记录", "status": "closed",
            "external_ref": "INSP-1"}, "inspector-user", "inspector")
        renewal = self._submit(item["id"])
        self.assertEqual(renewal["status"], "pending")
        self.assertEqual(renewal["permit_no"], "PERMIT-1")
        self.assertEqual(renewal["renewal_no"], "PERMIT-1-R1")
        decided = self.service.review_renewal(renewal["id"], {
            "decision": "approve", "expected_version": renewal["version"]},
            "manager-user", "compliance_manager")
        self.assertEqual(decided["status"], "approved")
        permit = self.service.get_item(item["id"], "viewer")
        self.assertEqual(permit["valid_from"], _days(30))
        self.assertEqual(permit["valid_until"], _days(395))
        self.assertFalse(permit["expired"])
        actions = [event["action"] for event in decided["events"]]
        self.assertEqual(actions, ["submit", "approve"])
        records = self.service.list_records(item["id"], "viewer")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["kind"], "inspection")
        audit_actions = [e["action"] for e in self.service.audit("viewer", item["id"])]
        self.assertIn("renewal_submit", audit_actions)
        self.assertIn("renewal_approve", audit_actions)
        self.assertTrue(self.repo.verify_audit_chain())
        follow_up = self._submit(item["id"])
        self.assertEqual(follow_up["renewal_no"], "PERMIT-1-R2")

    def test_duplicate_submission_conflict_and_resubmit_reuses_number(self):
        item = self._approved_item()
        renewal = self._submit(item["id"])
        with self.assertRaises(ConflictError) as ctx:
            self._submit(item["id"])
        self.assertIn(renewal["renewal_no"], str(ctx.exception))
        returned = self.service.review_renewal(renewal["id"], {
            "decision": "return", "comment": "缺少季度监测数据",
            "expected_version": renewal["version"]},
            "manager-user", "compliance_manager")
        self.assertEqual(returned["status"], "returned")
        with self.assertRaises(ConflictError) as ctx:
            self._submit(item["id"])
        self.assertIn(renewal["renewal_no"], str(ctx.exception))
        resubmitted = self.service.resubmit_renewal(renewal["id"], {
            "valid_from": _days(30), "valid_until": _days(395),
            "materials": ["排放监测报告", "季度监测数据"],
            "expected_version": returned["version"]},
            "applicant-user", "applicant")
        self.assertEqual(resubmitted["status"], "pending")
        self.assertEqual(resubmitted["renewal_no"], renewal["renewal_no"])
        self.assertEqual(resubmitted["materials"], ["排放监测报告", "季度监测数据"])
        decided = self.service.review_renewal(renewal["id"], {
            "decision": "approve", "expected_version": resubmitted["version"]},
            "manager-user", "compliance_manager")
        self.assertEqual(decided["status"], "approved")
        events = decided["events"]
        self.assertEqual([e["action"] for e in events],
                         ["submit", "return", "resubmit", "approve"])
        self.assertEqual(events[1]["comment"], "缺少季度监测数据")
        permit = self.service.get_item(item["id"], "viewer")
        self.assertEqual(permit["valid_until"], _days(395))

    def test_approve_blocked_when_materials_missing(self):
        item = self._approved_item()
        renewal = self._submit(item["id"])
        self.repo.conn.execute(
            "UPDATE renewals SET materials='[]' WHERE id=?", (renewal["id"],))
        with self.assertRaises(ConflictError) as ctx:
            self.service.review_renewal(renewal["id"], {
                "decision": "approve", "expected_version": renewal["version"]},
                "manager-user", "compliance_manager")
        self.assertIn("材料缺失", str(ctx.exception))

    def test_approve_blocked_when_permit_expired(self):
        item = self._approved_item(valid_until=_days(-1))
        renewal = self._submit(item["id"])
        with self.assertRaises(ConflictError) as ctx:
            self.service.review_renewal(renewal["id"], {
                "decision": "approve", "expected_version": renewal["version"]},
                "manager-user", "compliance_manager")
        self.assertIn("已过期", str(ctx.exception))
        detail = self.service.renewal_detail(renewal["id"], "viewer")
        self.assertEqual(detail["status"], "pending")

    def test_submit_requires_approved_permit(self):
        item = self.service.create_item({
            "title": "draft permit", "description": "not approved yet",
            "severity": "low", "quantity": 1, "threshold": 5,
            "external_ref": "PERMIT-DRAFT", "valid_until": _days(30)},
            "creator", "applicant")
        with self.assertRaises(ConflictError):
            self._submit(item["id"])

    def test_role_version_and_comment_guards(self):
        item = self._approved_item()
        with self.assertRaises(PermissionDenied):
            self.service.submit_renewal(item["id"], {
                "valid_until": _days(395), "materials": ["材料"]},
                "inspector-user", "inspector")
        renewal = self._submit(item["id"])
        with self.assertRaises(PermissionDenied):
            self.service.review_renewal(renewal["id"], {
                "decision": "approve", "expected_version": renewal["version"]},
                "applicant-user", "applicant")
        with self.assertRaises(ValidationError):
            self.service.review_renewal(renewal["id"], {
                "decision": "return", "expected_version": renewal["version"]},
                "manager-user", "compliance_manager")
        with self.assertRaises(ConflictError):
            self.service.review_renewal(renewal["id"], {
                "decision": "approve", "expected_version": 99},
                "manager-user", "compliance_manager")
        with self.assertRaises(ConflictError):
            self.service.resubmit_renewal(renewal["id"], {
                "valid_until": _days(395), "materials": ["材料"],
                "expected_version": renewal["version"]},
                "applicant-user", "applicant")
        returned = self.service.review_renewal(renewal["id"], {
            "decision": "return", "comment": "请补充材料",
            "expected_version": renewal["version"]},
            "manager-user", "compliance_manager")
        with self.assertRaises(ConflictError):
            self.service.resubmit_renewal(renewal["id"], {
                "valid_until": _days(395), "materials": ["材料"],
                "expected_version": renewal["version"]},
                "applicant-user", "applicant")
        self.assertEqual(returned["status"], "returned")

    def test_submission_validation(self):
        item = self._approved_item()
        with self.assertRaises(ValidationError):
            self._submit(item["id"], materials=[])
        with self.assertRaises(ValidationError):
            self._submit(item["id"], valid_until=None)
        with self.assertRaises(ValidationError):
            self._submit(item["id"], valid_from=_days(400), valid_until=_days(395))
        with self.assertRaises(ValidationError):
            self._submit(item["id"], valid_until="2026/01/01")

    def test_permit_no_defaults_and_listing(self):
        item = self.service.create_item({
            "title": "no ref", "description": "without external ref",
            "severity": "low", "quantity": 1, "threshold": 5},
            "creator", "applicant")
        self.assertEqual(item["permit_no"], f"AQ{item['id']}")
        self.assertIsNone(item["valid_until"])
        self.assertFalse(item["expired"])
        approved = self._approved_item(external_ref="PERMIT-9")
        self._submit(approved["id"])
        renewals = self.service.list_renewals(approved["id"], "viewer")
        self.assertEqual(len(renewals), 1)
        self.assertEqual(renewals[0]["permit_status"], "approved")
        self.assertFalse(renewals[0]["permit_expired"])
        self.assertEqual(len(self.service.list_renewals(None, "viewer")), 1)


if __name__ == "__main__":
    unittest.main()
