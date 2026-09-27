import tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES, permit_expired, renewal_blockers


def future_date(days):
    return (datetime.now(timezone.utc) + timedelta(days=days)).date().isoformat()


def past_date(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


class RenewalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self._approved_permit("RN-PERMIT-1", future_date(365))

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _approved_permit(self, permit_no, valid_until):
        item = self.service.create_item({
            "title": "permit", "description": "renewal target", "severity": "medium",
            "quantity": 1, "threshold": 10, "external_ref": permit_no,
            "valid_from": "2020-01-01", "valid_until": valid_until,
        }, "applicant", "applicant")
        for target in ("submitted", "inspection", "correction"):
            item = self.service.transition(
                item["id"], target, item["version"], "u",
                TRANSITION_ROLES[target][0])
        self.service.add_record(item["id"], {
            "kind": "inspection", "detail": "现场检查", "status": "closed",
        }, "inspector", "inspector")
        return self.service.transition(
            item["id"], "approved", item["version"], "mgr",
            TRANSITION_ROLES["approved"][0])

    def _materials(self, *names):
        return [{"name": n} for n in names]

    def test_full_renewal_cycle_keeps_number_and_history(self):
        item = self.item
        renewal = self.service.submit_renewal(item["id"], {
            "permit_no": "RN-PERMIT-1", "expected_version": item["version"],
            "valid_from": future_date(1), "valid_until": future_date(1800),
            "materials": self._materials("申请表", "监测报告"),
        }, "applicant", "applicant")
        renewal_no = renewal["renewal_no"]
        self.assertEqual(renewal["status"], "submitted")
        self.assertEqual(len(renewal["materials"]), 2)

        # 合规负责人核对后退回，意见留存
        returned = self.service.review_renewal(item["id"], renewal_no, {
            "decision": "return", "opinion": "监测报告缺盖章",
        }, "mgr", "compliance_manager")
        self.assertEqual(returned["status"], "returned")

        # 补件再提交沿用原编号
        resubmitted = self.service.supplement_renewal(item["id"], renewal_no, {
            "materials": self._materials("盖章版监测报告"),
        }, "applicant", "applicant")
        self.assertEqual(resubmitted["renewal_no"], renewal_no)
        self.assertEqual(resubmitted["status"], "submitted")
        self.assertEqual(len(resubmitted["materials"]), 3)

        # 批准后新期限写入许可，许可版本递增
        approved = self.service.review_renewal(item["id"], renewal_no, {
            "decision": "approve", "opinion": "材料齐备",
        }, "mgr", "compliance_manager")
        permit = approved["permit"]
        self.assertEqual(permit["valid_until"], future_date(1800))
        self.assertEqual(permit["version"], item["version"] + 1)

        # 历次意见都在详情
        detail = self.service.get_renewal(item["id"], renewal_no, "viewer")
        self.assertEqual([r["action"] for r in detail["reviews"]], ["return", "approve"])
        self.assertEqual(detail["reviews"][0]["opinion"], "监测报告缺盖章")

        # 原检查记录仍可查
        self.assertEqual(len(self.service.list_records(item["id"], "viewer")), 1)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_only_one_pending_renewal_per_permit(self):
        item = self.item
        first = self.service.submit_renewal(item["id"], {
            "permit_no": "RN-PERMIT-1", "expected_version": item["version"],
            "valid_from": future_date(1), "valid_until": future_date(1800),
            "materials": self._materials("申请表"),
        }, "applicant", "applicant")
        with self.assertRaises(ConflictError):
            self.service.submit_renewal(item["id"], {
                "permit_no": "RN-PERMIT-1", "expected_version": item["version"],
                "valid_from": future_date(1), "valid_until": future_date(1800),
                "materials": self._materials("再次提交"),
            }, "applicant", "applicant")
        # 批准后可发起新一轮，编号自然延续
        self.service.review_renewal(item["id"], first["renewal_no"], {
            "decision": "approve",
        }, "mgr", "compliance_manager")
        permit = self.service.get_item(item["id"], "viewer")
        second = self.service.submit_renewal(permit["id"], {
            "permit_no": "RN-PERMIT-1", "expected_version": permit["version"],
            "valid_from": future_date(1801), "valid_until": future_date(3600),
            "materials": self._materials("新一轮材料"),
        }, "applicant", "applicant")
        self.assertNotEqual(second["renewal_no"], first["renewal_no"])
        self.assertEqual(len(self.service.list_renewals(item["id"], "viewer")), 2)

    def test_wrong_permit_number_or_version_rejected(self):
        item = self.item
        with self.assertRaises(ValidationError):
            self.service.submit_renewal(item["id"], {
                "permit_no": "OTHER-PERMIT", "expected_version": item["version"],
                "valid_from": future_date(1), "valid_until": future_date(1800),
                "materials": self._materials("申请表"),
            }, "applicant", "applicant")
        with self.assertRaises(ConflictError):
            self.service.submit_renewal(item["id"], {
                "permit_no": "RN-PERMIT-1", "expected_version": item["version"] + 1,
                "valid_from": future_date(1), "valid_until": future_date(1800),
                "materials": self._materials("申请表"),
            }, "applicant", "applicant")

    def test_missing_materials_or_expired_permit_cannot_approve(self):
        expired = self._approved_permit("RN-EXPIRED-1", past_date(2))
        renewal = self.service.submit_renewal(expired["id"], {
            "permit_no": "RN-EXPIRED-1", "expected_version": expired["version"],
            "valid_from": future_date(1), "valid_until": future_date(1800),
            "materials": self._materials("申请表"),
        }, "applicant", "applicant")
        with self.assertRaises(ConflictError) as ctx:
            self.service.review_renewal(expired["id"], renewal["renewal_no"], {
                "decision": "approve",
            }, "mgr", "compliance_manager")
        self.assertIn("原许可已过期", str(ctx.exception))
        # 被拒后申请仍是待审，许可期限与版本未被改动
        self.assertEqual(
            self.service.get_renewal(expired["id"], renewal["renewal_no"], "viewer")["status"],
            "submitted")
        self.assertEqual(self.service.get_item(expired["id"], "viewer")["version"],
                         expired["version"])
        self.assertTrue(renewal_blockers([], future_date(10)))

    def test_return_requires_opinion_and_only_submitted_can_review(self):
        item = self.item
        renewal = self.service.submit_renewal(item["id"], {
            "permit_no": "RN-PERMIT-1", "expected_version": item["version"],
            "valid_from": future_date(1), "valid_until": future_date(1800),
            "materials": self._materials("申请表"),
        }, "applicant", "applicant")
        with self.assertRaises(ValidationError):
            self.service.review_renewal(item["id"], renewal["renewal_no"], {
                "decision": "return",
            }, "mgr", "compliance_manager")
        self.service.review_renewal(item["id"], renewal["renewal_no"], {
            "decision": "return", "opinion": "补正",
        }, "mgr", "compliance_manager")
        with self.assertRaises(ConflictError):
            self.service.review_renewal(item["id"], renewal["renewal_no"], {
                "decision": "approve",
            }, "mgr", "compliance_manager")

    def test_roles_and_prerequisites(self):
        item = self.item
        payload = {
            "permit_no": "RN-PERMIT-1", "expected_version": item["version"],
            "valid_from": future_date(1), "valid_until": future_date(1800),
            "materials": self._materials("申请表"),
        }
        with self.assertRaises(PermissionDenied):
            self.service.submit_renewal(item["id"], payload, "x", "viewer")
        renewal = self.service.submit_renewal(
            item["id"], payload, "applicant", "applicant")
        with self.assertRaises(PermissionDenied):
            self.service.review_renewal(item["id"], renewal["renewal_no"], {
                "decision": "approve",
            }, "applicant", "applicant")
        # 未批准的许可不能续期
        draft = self.service.create_item({
            "title": "draft", "description": "x", "severity": "low",
            "quantity": 1, "threshold": 10, "external_ref": "RN-DRAFT-1",
        }, "applicant", "applicant")
        with self.assertRaises(ConflictError):
            self.service.submit_renewal(draft["id"], {
                "permit_no": "RN-DRAFT-1", "expected_version": 1,
                "valid_from": future_date(1), "valid_until": future_date(1800),
                "materials": self._materials("申请表"),
            }, "applicant", "applicant")

    def test_expiry_rule(self):
        self.assertTrue(permit_expired(past_date(1)))
        self.assertFalse(permit_expired(future_date(10)))
        self.assertFalse(permit_expired(None))
        with self.assertRaises(ValidationError):
            self.service.submit_renewal(self.item["id"], {
                "permit_no": "RN-PERMIT-1", "expected_version": self.item["version"],
                "valid_from": future_date(10), "valid_until": future_date(1),
                "materials": self._materials("申请表"),
            }, "applicant", "applicant")


if __name__ == "__main__":
    unittest.main()
