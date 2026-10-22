from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db, utcnow

# ---------------------------------------------------------------------------
# Roles & permissions (two-layer authorisation: role -> area, permission -> action)
# ---------------------------------------------------------------------------
ROLES = ["super_admin", "ceo", "tech_head", "hr", "manager", "recruiter", "employee"]
ROLE_LABELS = {
    "super_admin": "Administrator", "ceo": "CEO", "tech_head": "Tech Head",
    "hr": "HR", "manager": "Manager", "recruiter": "Recruiter", "employee": "Employee",
}

PERMISSIONS = {
    "manage_users": "Create, edit, disable users and reset passwords",
    "manage_departments": "Create and manage departments",
    "manage_permissions": "Edit the role-permission matrix",
    "view_activity_logs": "View the admin activity / audit log",
    "view_attrition_dashboard": "View the AI attrition dashboard and risk cards",
    "run_predictions": "Run AI attrition predictions",
    "upload_excel": "Upload employee Excel files to the AI pipeline",
    "what_if_simulation": "Use the What-If retention simulator",
    "export_reports": "Export risk reports (CSV)",
    "manage_retention_actions": "Create and update retention action plans",
    "view_executive_dashboard": "View the executive (CEO) overview",
    "view_team": "View team members, performance and risk",
    "manage_recruitment": "Post and close job opportunities",
    "review_applications": "Review, shortlist and reject applications",
    "schedule_interviews": "Schedule interviews and record feedback",
    "send_offers": "Send job offers",
    "post_announcements": "Post pinned announcements in the forum",
    "moderate_forum": "Delete forum posts and comments",
    "use_ai_assistant": "Use the AI HR Assistant",
}

DEFAULT_ROLE_PERMISSIONS = {
    "super_admin": list(PERMISSIONS),
    "ceo": ["view_executive_dashboard", "export_reports", "use_ai_assistant", "post_announcements"],
    "tech_head": ["view_team", "use_ai_assistant", "manage_retention_actions"],
    "hr": ["view_attrition_dashboard", "run_predictions", "upload_excel", "what_if_simulation",
           "export_reports", "manage_retention_actions", "manage_recruitment", "review_applications",
           "schedule_interviews", "send_offers", "post_announcements", "moderate_forum",
           "use_ai_assistant"],
    "manager": ["view_team", "use_ai_assistant", "manage_retention_actions"],
    "recruiter": ["manage_recruitment", "review_applications", "schedule_interviews", "send_offers"],
    "employee": [],
}


class RolePermission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    role = db.Column(db.String(20), nullable=False, index=True)
    permission = db.Column(db.String(50), nullable=False)
    __table_args__ = (db.UniqueConstraint("role", "permission"),)


class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)

    def __repr__(self):
        return f"<Department {self.name}>"


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"))
    manager_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    is_active_flag = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)
    last_login_at = db.Column(db.DateTime)
    failed_login_count = db.Column(db.Integer, default=0)
    locked_until = db.Column(db.DateTime)
    must_change_password = db.Column(db.Boolean, default=False)

    department = db.relationship("Department", backref="users")
    manager = db.relationship("User", remote_side=[id], backref="direct_reports")
    employee_profile = db.relationship("Employee", backref="user", uselist=False)

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        try:
            return check_password_hash(self.password_hash, raw)
        except (ValueError, TypeError):      # unusable marker / malformed hash
            return False

    @property
    def is_active(self):
        return bool(self.is_active_flag)

    @property
    def is_locked(self):
        return bool(self.locked_until and self.locked_until > utcnow())

    @property
    def role_label(self):
        return ROLE_LABELS.get(self.role, self.role.replace("_", " ").title())

    @property
    def initials(self):
        parts = (self.name or "?").split()
        return (parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")).upper()

    def __repr__(self):
        return f"<User {self.email} ({self.role})>"


class ActivityLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    action = db.Column(db.String(255))
    category = db.Column(db.String(30), default="general")   # auth/admin/ai/data/hiring/forum/security
    ip_address = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=utcnow, index=True)
    user = db.relationship("User")


# ---------------------------------------------------------------------------
# Employee / attrition domain
# ---------------------------------------------------------------------------
class Employee(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), unique=True)
    employee_code = db.Column(db.String(20), unique=True)
    job_role = db.Column(db.String(120))
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"))

    age = db.Column(db.Integer)
    monthly_salary = db.Column(db.Float)
    years_at_company = db.Column(db.Float)
    job_satisfaction = db.Column(db.Integer)       # 1-4
    work_life_balance = db.Column(db.Integer)      # 1-4
    overtime = db.Column(db.Boolean, default=False)
    performance_rating = db.Column(db.Integer)     # 1-5
    years_since_promotion = db.Column(db.Float)
    training_times_last_year = db.Column(db.Integer)
    distance_from_home = db.Column(db.Float)
    num_companies_worked = db.Column(db.Integer)
    salary_hike_percent = db.Column(db.Float)
    skills = db.Column(db.String(255), default="")  # "Python:advanced,SQL:basic"

    department = db.relationship("Department")

    @property
    def name(self):
        return self.user.name if self.user else self.employee_code

    @property
    def annual_salary(self):
        return (self.monthly_salary or 0) * 12

    def to_feature_dict(self):
        return {
            "Age": self.age, "MonthlySalary": self.monthly_salary,
            "YearsAtCompany": self.years_at_company, "JobSatisfaction": self.job_satisfaction,
            "WorkLifeBalance": self.work_life_balance, "OverTime": 1 if self.overtime else 0,
            "PerformanceRating": self.performance_rating,
            "YearsSincePromotion": self.years_since_promotion,
            "TrainingTimesLastYear": self.training_times_last_year,
            "DistanceFromHome": self.distance_from_home,
            "NumCompaniesWorked": self.num_companies_worked,
            "SalaryHikePercent": self.salary_hike_percent,
        }


class UploadBatch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255))
    uploaded_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=utcnow)
    rows_total = db.Column(db.Integer, default=0)
    rows_valid = db.Column(db.Integer, default=0)
    rows_rejected = db.Column(db.Integer, default=0)
    employees_created = db.Column(db.Integer, default=0)
    employees_updated = db.Column(db.Integer, default=0)
    high_risk = db.Column(db.Integer, default=0)
    medium_risk = db.Column(db.Integer, default=0)
    low_risk = db.Column(db.Integer, default=0)
    report_json = db.Column(db.Text)
    uploader = db.relationship("User")


class AttritionPrediction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), index=True)
    probability = db.Column(db.Float)
    risk_score = db.Column(db.Integer)
    risk_level = db.Column(db.String(10))
    top_factors = db.Column(db.Text)          # JSON list[str]
    recommendations = db.Column(db.Text)      # JSON list[str]
    explanations = db.Column(db.Text)         # JSON list[{label, impact}]
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    source = db.Column(db.String(30), default="manual")
    batch_id = db.Column(db.Integer, db.ForeignKey("upload_batch.id"), nullable=True)
    employee = db.relationship("Employee", backref="predictions")


class AttritionSnapshot(db.Model):
    """Monthly department-level history used for the Attrition Trend chart."""
    id = db.Column(db.Integer, primary_key=True)
    month = db.Column(db.Date, index=True)           # first day of month
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"))
    headcount = db.Column(db.Integer)
    avg_risk = db.Column(db.Float)
    high_risk = db.Column(db.Integer)
    exits = db.Column(db.Integer)                   # actual resignations that month
    department = db.relationship("Department")


class RetentionAction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.Integer, db.ForeignKey("employee.id"), index=True)
    action = db.Column(db.String(255))
    status = db.Column(db.String(20), default="planned")   # planned / in_progress / done
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    due_date = db.Column(db.Date)
    created_at = db.Column(db.DateTime, default=utcnow)
    employee = db.relationship("Employee", backref="retention_actions")
    owner = db.relationship("User")


# ---------------------------------------------------------------------------
# Recruitment / career module
# ---------------------------------------------------------------------------
APPLICATION_FLOW = ["submitted", "under_review", "shortlisted", "interview", "selected", "offer"]
APPLICATION_LABELS = {
    "submitted": "Applied", "under_review": "Under Review", "shortlisted": "Shortlisted",
    "interview": "Interview", "selected": "Selected", "offer": "Offer",
    "accepted": "Offer Accepted", "declined": "Offer Declined", "rejected": "Not Selected",
}


class Opportunity(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150))
    description = db.Column(db.Text)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"))
    required_skills = db.Column(db.String(255))
    posted_by = db.Column(db.Integer, db.ForeignKey("user.id"))
    is_internal = db.Column(db.Boolean, default=True)
    status = db.Column(db.String(20), default="open")
    created_at = db.Column(db.DateTime, default=utcnow)
    department = db.relationship("Department")


class Application(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    opportunity_id = db.Column(db.Integer, db.ForeignKey("opportunity.id"))
    applicant_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    status = db.Column(db.String(30), default="submitted")
    applied_at = db.Column(db.DateTime, default=utcnow)
    opportunity = db.relationship("Opportunity", backref="applications")
    applicant = db.relationship("User")

    @property
    def status_label(self):
        return APPLICATION_LABELS.get(self.status, self.status.title())

    @property
    def stage_index(self):
        return APPLICATION_FLOW.index(self.status) if self.status in APPLICATION_FLOW else (
            len(APPLICATION_FLOW) - 1 if self.status in ("accepted", "declined") else -1)


class Interview(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("application.id"))
    scheduled_at = db.Column(db.DateTime)
    interviewer = db.Column(db.String(120))
    feedback = db.Column(db.Text)
    result = db.Column(db.String(20), default="pending")
    application = db.relationship("Application", backref="interviews")


class Offer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("application.id"))
    offered_salary = db.Column(db.Float)
    status = db.Column(db.String(20), default="pending")
    created_at = db.Column(db.DateTime, default=utcnow)
    application = db.relationship("Application", backref=db.backref("offer", uselist=False))


# ---------------------------------------------------------------------------
# Forum & notifications
# ---------------------------------------------------------------------------
class ForumCategory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True)


class ForumPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    category_id = db.Column(db.Integer, db.ForeignKey("forum_category.id"))
    title = db.Column(db.String(200))
    content = db.Column(db.Text)
    is_anonymous = db.Column(db.Boolean, default=False)
    is_announcement = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    author = db.relationship("User")
    category = db.relationship("ForumCategory")

    @property
    def like_count(self):
        return ForumLike.query.filter_by(post_id=self.id).count()

    @property
    def comment_count(self):
        return ForumComment.query.filter_by(post_id=self.id).count()


class ForumComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("forum_post.id"))
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    content = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow)
    is_hr_response = db.Column(db.Boolean, default=False)
    post = db.relationship("ForumPost", backref="comments")
    author = db.relationship("User")


class ForumLike(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    post_id = db.Column(db.Integer, db.ForeignKey("forum_post.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    __table_args__ = (db.UniqueConstraint("post_id", "user_id"),)


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    message = db.Column(db.String(255))
    link = db.Column(db.String(255), default="#")
    kind = db.Column(db.String(20), default="info")   # info/interview/application/forum/risk/offer
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    user = db.relationship("User")


def notify(user_id, message, link="#", kind="info"):
    if not user_id:
        return None
    n = Notification(user_id=user_id, message=message[:255], link=link, kind=kind)
    db.session.add(n)
    return n


def create_default_employee(user):
    emp = Employee(
        user_id=user.id, employee_code=f"EMP{user.id:04d}", department_id=user.department_id,
        job_role="Not set", age=25, monthly_salary=30000, years_at_company=0,
        job_satisfaction=3, work_life_balance=3, overtime=False, performance_rating=3,
        years_since_promotion=0, training_times_last_year=0, distance_from_home=5,
        num_companies_worked=0, salary_hike_percent=10, skills="",
    )
    db.session.add(emp)
    return emp
