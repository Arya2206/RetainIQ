"""Automated test-suite for RetainIQ.  Run:  python -m unittest discover -v tests"""
import io
import os
import re
import tempfile
import unittest

_tmp = tempfile.mkdtemp()
os.environ["RETAINIQ_DB"] = os.path.join(_tmp, "test.db")
os.environ.pop("DATABASE_URL", None)

import pandas as pd                      # noqa: E402
from app import create_app               # noqa: E402
from extensions import db                # noqa: E402
from models import User, RolePermission, Employee, ActivityLog, AttritionPrediction  # noqa: E402

CREDS = {
    "admin": ("admin@retainiq.com", "Admin@123"), "ceo": ("ceo@retainiq.com", "Ceo@12345"),
    "tech_head": ("techhead@retainiq.com", "Tech@1234"), "hr": ("hr@retainiq.com", "Hr@12345"),
    "manager": ("manager@retainiq.com", "Manager@123"), "recruiter": ("recruiter@retainiq.com", "Recruit@123"),
    "employee": ("employee@retainiq.com", "Employee@123"),
}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config.update(TESTING=True)

    def setUp(self):
        self.c = self.app.test_client()

    def csrf(self):
        """Return this client's CSRF token (creating one in its session if needed)."""
        with self.c.session_transaction() as s:
            s.setdefault("_csrf", "test-csrf-token")
            return s["_csrf"]

    def post(self, path, data=None, **kw):
        data = dict(data or {})
        data["csrf_token"] = self.csrf()
        return self.c.post(path, data=data, **kw)

    def login(self, who):
        e, p = CREDS[who]
        r = self.post("/login", {"email": e, "password": p}, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        return r


class TestAuth(Base):
    def test_every_role_can_sign_in_and_lands_on_own_dashboard(self):
        landing = {"admin": "System Analytics", "ceo": "Executive overview", "tech_head": "department",
                   "hr": "AI Attrition Dashboard", "manager": "My team", "recruiter": "Recruitment overview",
                   "employee": "Welcome"}
        for who, text in landing.items():
            c = self.app.test_client(); self.c = c
            self.login(who)
            html = c.get("/dashboard", follow_redirects=True).get_data(as_text=True)
            self.assertIn(text, html, who)

    def test_wrong_password_rejected(self):
        r = self.post("/login", {"email": "hr@retainiq.com", "password": "nope"})
        self.assertIn(b"Invalid email or password", r.data)

    def test_csrf_blocks_post_without_token(self):
        r = self.c.post("/login", data={"email": "hr@retainiq.com", "password": "Hr@12345"})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/login", data={"email": "hr@retainiq.com", "password": "Hr@12345", "csrf_token": "forged"})
        self.assertEqual(r.status_code, 400)

    def test_lockout_after_five_failures(self):
        for _ in range(5):
            self.post("/login", {"email": "recruiter@retainiq.com", "password": "bad"})
        r = self.post("/login", {"email": "recruiter@retainiq.com", "password": "Recruit@123"})
        self.assertEqual(r.status_code, 429)
        with self.app.app_context():                       # unlock for other tests
            u = User.query.filter_by(email="recruiter@retainiq.com").first()
            u.locked_until, u.failed_login_count = None, 0
            db.session.commit()

    def test_registration_rules(self):
        base = {"name": "Test Student", "password": "Passw0rdX", "confirm": "Passw0rdX", "department_id": ""}
        r = self.post("/register", dict(base, email="stu1@example.com", role="employee"), follow_redirects=True)
        self.assertIn(b"Account created", r.data)
        r = self.post("/register", dict(base, email="stu2@example.com", role="hr", access_code=""))
        self.assertIn(b"Invalid staff access code", r.data)
        r = self.post("/register", dict(base, email="stu3@example.com", role="hr", access_code="RETAINIQ2026"), follow_redirects=True)
        self.assertIn(b"Account created", r.data)
        r = self.post("/register", dict(base, email="stu1@example.com", role="employee"))
        self.assertIn(b"already exists", r.data)
        r = self.post("/register", dict(base, email="stu4@example.com", role="employee", password="short", confirm="short"))
        self.assertIn(b"at least 8", r.data)

    def test_security_headers_present(self):
        r = self.c.get("/login")
        for h in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options"):
            self.assertIn(h, r.headers)

    def test_logout_requires_post(self):
        self.login("hr")
        self.assertEqual(self.c.get("/logout").status_code, 405)


class TestRBAC(Base):
    FORBIDDEN = {
        "employee": ["/hr/", "/hr/employees", "/admin/", "/ceo/", "/tech/", "/manager/", "/recruitment/", "/hr/model"],
        "manager": ["/hr/", "/admin/", "/ceo/", "/admin/permissions", "/recruitment/"],
        "recruiter": ["/hr/", "/admin/", "/ceo/", "/manager/"],
        "hr": ["/admin/", "/ceo/", "/admin/users"],
        "ceo": ["/hr/", "/admin/", "/manager/"],
        "tech_head": ["/hr/", "/admin/", "/ceo/"],
    }

    def test_url_tampering_gives_403(self):
        for who, urls in self.FORBIDDEN.items():
            self.c = self.app.test_client(); self.login(who)
            for u in urls:
                self.assertEqual(self.c.get(u).status_code, 403, f"{who} should not open {u}")

    def test_anonymous_redirected_to_login(self):
        r = self.c.get("/hr/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_all_role_pages_render(self):
        pages = {
            "admin": ["/admin/", "/admin/users", "/admin/permissions", "/admin/departments", "/admin/logs", "/hr/", "/hr/upload", "/hr/model", "/ceo/", "/recruitment/"],
            "ceo": ["/ceo/", "/forum/", "/notifications", "/account"],
            "tech_head": ["/tech/"], "manager": ["/manager/"],
            "hr": ["/hr/", "/hr/employees", "/hr/employees?q=a&risk=High", "/hr/upload", "/hr/model", "/recruitment/", "/recruitment/interviews", "/recruitment/offers", "/forum/"],
            "recruiter": ["/recruitment/", "/recruitment/opportunities/create", "/recruitment/interviews", "/recruitment/offers"],
            "employee": ["/employee/", "/employee/career", "/employee/opportunities", "/employee/applications", "/employee/interviews", "/employee/offers", "/forum/", "/forum/create", "/notifications", "/account/password"],
        }
        for who, urls in pages.items():
            self.c = self.app.test_client(); self.login(who)
            for u in urls:
                self.assertEqual(self.c.get(u).status_code, 200, f"{who} {u}")

    def test_manager_sees_only_own_team(self):
        self.login("manager")
        with self.app.app_context():
            other = Employee.query.join(User, Employee.user_id == User.id).filter(User.manager_id.is_(None)).first().id
        self.assertEqual(self.c.get(f"/manager/employee/{other}").status_code, 403)

    def test_permission_matrix_changes_access(self):
        self.login("recruiter")
        with self.app.app_context():
            RolePermission.query.filter_by(role="recruiter", permission="send_offers").delete()
            db.session.commit()
        r = self.c.post("/recruitment/applications/1/offer", data={"csrf_token": self.csrf(), "offered_salary": "50000"})
        self.assertEqual(r.status_code, 403)
        with self.app.app_context():
            db.session.add(RolePermission(role="recruiter", permission="send_offers"))
            db.session.commit()


class TestAI(Base):
    def test_api_dashboard_filters_and_trend(self):
        self.login("hr")
        d = self.c.get("/api/v1/hr/dashboard").get_json()
        self.assertGreater(d["kpis"]["employees"], 50)
        self.assertEqual(sum(d["distribution"]["counts"]), d["kpis"]["analyzed"])
        self.assertGreaterEqual(len(d["trend"]["labels"]), 12)
        it = self.c.get("/api/v1/hr/dashboard?dept=IT").get_json()
        self.assertLess(it["kpis"]["employees"], d["kpis"]["employees"])
        hi = self.c.get("/api/v1/hr/dashboard?risk=High").get_json()
        self.assertTrue(all(t["level"] == "High" for t in hi["top"]))

    def test_what_if_reduces_risk(self):
        self.login("hr")
        with self.app.app_context():
            eid = (AttritionPrediction.query.filter_by(risk_level="High").first().employee_id)
        r = self.c.post("/api/v1/predict/what-if", json={"employee_id": eid, "changes": {
            "OverTime": 0, "JobSatisfaction": 4, "SalaryHikePercent": 25, "WorkLifeBalance": 4}},
            headers={"X-CSRF-Token": self.csrf()}).get_json()
        self.assertLess(r["simulated"], r["current"])

    def test_assistant_answers_and_is_scoped(self):
        self.login("hr")
        h = {"X-CSRF-Token": self.csrf()}
        a = self.c.post("/api/v1/assistant", json={"question": "Which department has the highest attrition risk?"}, headers=h).get_json()
        self.assertIn("ranking", a["answer"])
        a = self.c.post("/api/v1/assistant", json={"question": "Show employees with high attrition risk and low job satisfaction"}, headers=h).get_json()
        self.assertTrue(all(i["level"] == "High" for i in a["items"]))
        self.c = self.app.test_client(); self.login("manager")
        a = self.c.post("/api/v1/assistant", json={"question": "how many employees"}, headers={"X-CSRF-Token": self.csrf()}).get_json()
        self.assertIn("your team", a["answer"])
        self.c = self.app.test_client(); self.login("employee")
        self.assertEqual(self.c.post("/api/v1/assistant", json={"question": "hi"}, headers={"X-CSRF-Token": self.csrf()}).status_code, 403)

    def test_employee_cannot_read_risk_api(self):
        self.login("employee")
        self.assertEqual(self.c.get("/api/v1/employees/1/risk").status_code, 403)

    def test_health_endpoint(self):
        j = self.c.get("/api/v1/health").get_json()
        self.assertEqual(j["status"], "ok")

    def test_model_quality_floor(self):
        from ml.predictor import model_info
        m = model_info()["metrics"]
        self.assertGreater(m["roc_auc"], 0.80)
        self.assertGreater(m["pr_auc"], 0.55)
        self.assertGreater(m["accuracy"], m["majority_baseline_accuracy"])   # must beat "nobody leaves"
        self.assertLess(m["brier"], 0.15)                                    # probabilities are calibrated
        self.assertNotIn("Age", m["features"])                               # protected attribute not used
        self.assertIn("fairness", m)


class TestExcelPipeline(Base):
    def _xlsx(self, df):
        b = io.BytesIO(); df.to_excel(b, index=False); b.seek(0); return b

    def test_pipeline_cleans_validates_and_predicts(self):
        from services.excel_pipeline import sample_workbook_bytes
        self.login("hr")
        r = self.post("/hr/upload", {"file": (sample_workbook_bytes(30), "emp.xlsx")}, content_type="multipart/form-data", follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertIn("employees analysed", html)
        self.assertIn("Row 9", html)                       # the deliberately invalid email row
        self.assertIn("high-risk employees detected", html)

    def test_missing_columns_rejected(self):
        self.login("hr")
        bad = self._xlsx(pd.DataFrame({"Name": ["A"], "Email": ["a@b.com"]}))
        r = self.post("/hr/upload", {"file": (bad, "bad.xlsx")}, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn(b"Missing required column", r.data)

    def test_wrong_file_type_rejected(self):
        self.login("hr")
        r = self.post("/hr/upload", {"file": (io.BytesIO(b"x"), "evil.exe")}, content_type="multipart/form-data", follow_redirects=True)
        self.assertIn(b"Only .xlsx", r.data)

    def test_employee_cannot_upload(self):
        self.login("employee")
        self.assertEqual(self.c.get("/hr/upload").status_code, 403)


class TestHiringForumAdmin(Base):
    def test_full_hiring_flow(self):
        # employee applies -> recruiter reviews -> shortlists -> interview -> pass -> offer -> employee accepts
        self.login("employee")
        self.post("/employee/opportunities/4/apply")
        with self.app.app_context():
            from models import Application
            app_id = Application.query.filter_by(opportunity_id=4, applicant_id=User.query.filter_by(email="employee@retainiq.com").first().id).first().id
        self.c = self.app.test_client(); self.login("recruiter")
        self.post(f"/recruitment/applications/{app_id}/review", {"status": "under_review"})
        self.post(f"/recruitment/applications/{app_id}/review", {"status": "shortlisted"})
        self.post(f"/recruitment/applications/{app_id}/interview", {"interviewer": "Test", "scheduled_at": "2030-01-01T10:00"})
        with self.app.app_context():
            from models import Interview
            iv = Interview.query.filter_by(application_id=app_id).first().id
        self.post(f"/recruitment/interviews/{iv}/feedback", {"result": "pass", "feedback": "great"})
        self.post(f"/recruitment/applications/{app_id}/offer", {"offered_salary": "88000"})
        with self.app.app_context():
            from models import Application, Offer
            self.assertEqual(db.session.get(Application, app_id).status, "offer")
            oid = Offer.query.filter_by(application_id=app_id).first().id
        self.c = self.app.test_client(); self.login("employee")
        self.post(f"/employee/offers/{oid}/respond", {"decision": "accept"})
        with self.app.app_context():
            from models import Application
            self.assertEqual(db.session.get(Application, app_id).status, "accepted")

    def test_forum_xss_is_escaped_and_announcement_permission(self):
        self.login("employee")
        r = self.post("/forum/create", {"title": "<script>alert(1)</script>", "content": "x<img src=x onerror=alert(2)>", "category_id": "2"}, follow_redirects=True)
        html = r.get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertEqual(self.post("/forum/create", {"title": "t", "content": "c", "category_id": "1"}).status_code, 403)

    def test_admin_create_user_forces_password_change(self):
        self.login("admin")
        self.post("/admin/users/create", {"name": "Temp User", "email": "temp@retainiq.com", "role": "employee", "department_id": "", "password": "Temp@1234"}, follow_redirects=True)
        self.c = self.app.test_client()
        r = self.post("/login", {"email": "temp@retainiq.com", "password": "Temp@1234"}, follow_redirects=True)
        self.assertIn(b"Change password", r.data)

    def test_audit_log_records_actions(self):
        self.login("hr")
        with self.app.app_context():
            self.assertGreater(ActivityLog.query.filter_by(category="auth").count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
