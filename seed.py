"""Demo data for RetainIQ: departments, one account per role, ~120 employees scored by the
ML model, 12 months of attrition history, hiring pipeline, forum and notifications."""
import json
import random
from datetime import date, datetime, timedelta

from extensions import db, utcnow
from models import (User, Department, Employee, AttritionPrediction, AttritionSnapshot, RetentionAction,
                    Opportunity, Application, Interview, Offer, ForumCategory, ForumPost, ForumComment,
                    ForumLike, Notification, RolePermission, ActivityLog, DEFAULT_ROLE_PERMISSIONS,
                    create_default_employee)
from ml.predictor import predict_many, risk_level
from ml.train_model import generate_synthetic_dataset

DOMAIN = "retainiq.com"
FIRST = ["Rahul", "Priya", "Amit", "Sneha", "Vikram", "Anjali", "Rohan", "Kavya", "Arjun", "Neha", "Karan",
         "Divya", "Sanjay", "Pooja", "Manish", "Ritu", "Aditya", "Meera", "Nikhil", "Isha", "Varun", "Tanvi"]
LAST = ["Sharma", "Verma", "Patel", "Gupta", "Singh", "Iyer", "Nair", "Reddy", "Mehta", "Joshi", "Kapoor",
        "Rao", "Deshmukh", "Kulkarni", "Bose", "Chopra"]
DEPT_SIZES = {"IT": 28, "HR": 12, "Sales": 20, "Finance": 14, "Marketing": 14, "Operations": 18}
ROLES_BY_DEPT = {
    "IT": ["Software Developer", "QA Engineer", "DevOps Engineer", "Data Analyst"],
    "HR": ["HR Executive", "Recruiter", "HR Business Partner"],
    "Sales": ["Sales Executive", "Account Manager", "Sales Lead"],
    "Finance": ["Financial Analyst", "Accountant"],
    "Marketing": ["Marketing Executive", "Content Strategist"],
    "Operations": ["Operations Executive", "Process Analyst"],
}
SKILL_POOL = {"IT": ["Python", "SQL", "React", "Cloud", "Java"], "HR": ["Communication", "Excel", "SQL"],
              "Sales": ["Sales Negotiation", "Communication", "Excel"], "Finance": ["Excel", "SQL", "Communication"],
              "Marketing": ["Communication", "Excel", "SQL"], "Operations": ["Excel", "SQL", "Communication"]}


def run_seed(reset=True):
    rng = random.Random(7)
    if reset:
        db.drop_all()
    db.create_all()
    now = utcnow()

    # ---- permissions
    if RolePermission.query.count() == 0:
        for role, perms in DEFAULT_ROLE_PERMISSIONS.items():
            if role != "super_admin":
                for p in perms:
                    db.session.add(RolePermission(role=role, permission=p))

    # ---- departments & demo accounts
    depts = {}
    for name in DEPT_SIZES:
        d = Department(name=name)
        db.session.add(d)
        db.session.flush()
        depts[name] = d

    def mk(name, email, role, pw, dept):
        u = User(name=name, email=f"{email}@{DOMAIN}", role=role, department_id=depts[dept].id)
        u.set_password(pw)
        db.session.add(u)
        db.session.flush()
        return u

    admin = mk("Ananya Mehta", "admin", "super_admin", "Admin@123", "IT")
    ceo = mk("Rakesh Oberoi", "ceo", "ceo", "Ceo@12345", "IT")
    tech = mk("Vikram Rao", "techhead", "tech_head", "Tech@1234", "IT")
    hr = mk("Sunita Kapoor", "hr", "hr", "Hr@12345", "HR")
    rec = mk("Kiran Joshi", "recruiter", "recruiter", "Recruit@123", "HR")
    mgr = mk("Deepak Iyer", "manager", "manager", "Manager@123", "IT")
    mgr2 = mk("Farah Khan", "manager2", "manager", "Manager@123", "Sales")
    demo = mk("Priya Sharma", "employee", "employee", "Employee@123", "IT")

    # ---- employees (features drawn from the same distribution the model was trained on)
    pool = generate_synthetic_dataset(n=400, seed=2026).drop(columns=["Attrition"]).to_dict("records")
    people = [(demo, "IT")]
    used = set()
    for dname, n in DEPT_SIZES.items():
        for i in range(n - (1 if dname == "IT" else 0)):
            while True:
                nm = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
                em = f"{nm.split()[0].lower()}.{nm.split()[1].lower()}{rng.randint(1, 99)}@{DOMAIN}"
                if em not in used:
                    used.add(em)
                    break
            u = User(name=nm, email=em, role="employee", department_id=depts[dname].id)
            u.set_password("Welcome@123")
            db.session.add(u)
            db.session.flush()
            people.append((u, dname))

    emps = []
    team_count = {mgr.id: 0, mgr2.id: 0}
    for idx, (u, dname) in enumerate(people):
        f = pool[idx % len(pool)]
        if u is demo:     # the demo employee mirrors the brief's example career card
            f = dict(f, JobSatisfaction=2, OverTime=1, SalaryHikePercent=6, YearsSincePromotion=4.5, WorkLifeBalance=2)
        if u is demo:
            u.manager_id = mgr.id
            team_count[mgr.id] += 1
        elif dname == "IT" and team_count[mgr.id] < 9:
            u.manager_id = mgr.id
            team_count[mgr.id] += 1
        elif dname == "Sales" and team_count[mgr2.id] < 8:
            u.manager_id = mgr2.id
            team_count[mgr2.id] += 1
        pool_sk = SKILL_POOL[dname]
        if u is demo:
            skills = "Python:advanced,SQL:advanced,React:basic,Cloud:none"
        else:
            skills = ",".join(f"{s}:{rng.choices(['advanced', 'basic', 'none'], [0.4, 0.35, 0.25])[0]}"
                              for s in rng.sample(pool_sk, k=min(len(pool_sk), rng.randint(3, 4))))
        e = Employee(user_id=u.id, employee_code=f"EMP{u.id:04d}", job_role="Software Developer" if u is demo else rng.choice(ROLES_BY_DEPT[dname]),
                     department_id=depts[dname].id, age=int(f["Age"]), monthly_salary=float(f["MonthlySalary"]),
                     years_at_company=float(f["YearsAtCompany"]), job_satisfaction=int(f["JobSatisfaction"]),
                     work_life_balance=int(f["WorkLifeBalance"]), overtime=bool(f["OverTime"]),
                     performance_rating=int(f["PerformanceRating"]), years_since_promotion=float(f["YearsSincePromotion"]),
                     training_times_last_year=int(f["TrainingTimesLastYear"]), distance_from_home=float(f["DistanceFromHome"]),
                     num_companies_worked=int(f["NumCompaniesWorked"]), salary_hike_percent=float(f["SalaryHikePercent"]),
                     skills=skills)
        db.session.add(e)
        emps.append(e)
    db.session.flush()

    # ---- AI predictions + history (previous 3 monthly runs)
    results = predict_many([e.to_feature_dict() for e in emps])
    for e, r in zip(emps, results):
        for k in (3, 2, 1):
            s = max(0, min(100, r["risk_score"] + rng.randint(-9, 9) - (k - 2) * 2))
            db.session.add(AttritionPrediction(
                employee_id=e.id, probability=s / 100, risk_score=s,
                risk_level=risk_level(s),
                top_factors=json.dumps(r["top_factors"]), recommendations=json.dumps(r["recommendations"]),
                explanations=json.dumps(r["explanations"]), source="history", created_at=now - timedelta(days=30 * k)))
        db.session.add(AttritionPrediction(
            employee_id=e.id, probability=r["probability"], risk_score=r["risk_score"], risk_level=r["risk_level"],
            top_factors=json.dumps(r["top_factors"]), recommendations=json.dumps(r["recommendations"]),
            explanations=json.dumps(r["explanations"]), source="seed", created_at=now - timedelta(hours=2)))
    db.session.flush()

    # ---- 12 months of department snapshots (Attrition Trend)
    by_dept = {}
    for e, r in zip(emps, results):
        by_dept.setdefault(e.department_id, []).append(r["risk_score"])
    first_this_month = date.today().replace(day=1)
    for m in range(12, 0, -1):
        month = (first_this_month - timedelta(days=30 * m)).replace(day=1)
        for d in depts.values():
            sc = by_dept.get(d.id, [30])
            cur = sum(sc) / len(sc)
            avg = max(5, cur * (0.72 + 0.28 * (12 - m) / 11) + rng.uniform(-2.5, 2.5))
            hc = len(sc) + rng.randint(-2, 2)
            exits = max(0, int(round(hc * avg / 100 * rng.uniform(0.04, 0.10))))
            db.session.add(AttritionSnapshot(month=month, department_id=d.id, headcount=hc, avg_risk=round(avg, 1),
                                             high_risk=max(0, int(round(hc * avg / 100 * 0.45))), exits=exits))

    # ---- retention actions on the highest-risk employees
    ranked = sorted(zip(emps, results), key=lambda x: -x[1]["risk_score"])[:14]
    statuses = ["planned", "in_progress", "done", "in_progress", "planned"]
    for i, (e, r) in enumerate(ranked):
        for j, a in enumerate(r["recommendations"][:2]):
            db.session.add(RetentionAction(employee_id=e.id, action=a, owner_id=hr.id,
                                           status=statuses[(i + j) % len(statuses)],
                                           due_date=date.today() + timedelta(days=rng.randint(3, 30))))

    # ---- hiring pipeline
    specs = [("Senior Cloud Engineer", "IT", "Cloud,Python,SQL", "Own our cloud infrastructure, CI/CD and cost optimisation."),
             ("React Frontend Developer", "IT", "React,Python", "Build customer-facing analytics dashboards."),
             ("Data Platform Engineer", "IT", "Python,SQL,Cloud", "Design the data pipelines behind our HR analytics."),
             ("DevOps Lead", "IT", "Cloud,Python", "Lead platform reliability and release engineering."),
             ("HR Business Partner", "HR", "Communication,Excel", "Partner with department heads on people strategy."),
             ("Regional Sales Lead", "Sales", "Sales Negotiation,Communication", "Own a regional sales territory."),
             ("Financial Analyst II", "Finance", "Excel,SQL", "Support quarterly planning and forecasting.")]
    jobs = []
    for t, dn, sk, ds in specs:
        o = Opportunity(title=t, description=ds, department_id=depts[dn].id, required_skills=sk, posted_by=hr.id,
                        created_at=now - timedelta(days=rng.randint(2, 20)))
        db.session.add(o)
        jobs.append(o)
    db.session.flush()

    a1 = Application(opportunity_id=jobs[0].id, applicant_id=demo.id, status="interview", applied_at=now - timedelta(days=6))
    a2 = Application(opportunity_id=jobs[1].id, applicant_id=demo.id, status="offer", applied_at=now - timedelta(days=14))
    a3 = Application(opportunity_id=jobs[2].id, applicant_id=demo.id, status="shortlisted", applied_at=now - timedelta(days=3))
    db.session.add_all([a1, a2, a3])
    db.session.flush()
    tomorrow = (now + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)
    db.session.add(Interview(application_id=a1.id, scheduled_at=tomorrow, interviewer="Vikram Rao (Tech Head)"))
    db.session.add(Interview(application_id=a2.id, scheduled_at=now - timedelta(days=5), interviewer="Deepak Iyer",
                             feedback="Strong portfolio and clear communication.", result="pass"))
    db.session.add(Offer(application_id=a2.id, offered_salary=95000, created_at=now - timedelta(days=2)))
    for k, (u, _) in enumerate(people[1:7]):
        st = ["submitted", "under_review", "shortlisted", "submitted", "rejected", "under_review"][k]
        db.session.add(Application(opportunity_id=jobs[k % len(jobs)].id, applicant_id=u.id, status=st,
                                   applied_at=now - timedelta(days=rng.randint(1, 9))))

    # ---- forum
    cats = {}
    for n in ["Announcements", "WorkLifeBalance", "CareerGrowth", "CompanyPolicy", "AnonymousFeedback", "General"]:
        c = ForumCategory(name=n)
        db.session.add(c)
        db.session.flush()
        cats[n] = c
    ann = ForumPost(author_id=hr.id, category_id=cats["Announcements"].id, is_announcement=True,
                    title="Hybrid working policy update — effective next month",
                    content="Employees may now work remotely up to two days a week with manager approval. "
                            "Raise a request through your manager; HR will confirm within 3 working days.",
                    created_at=now - timedelta(days=2))
    p1 = ForumPost(author_id=demo.id, category_id=cats["WorkLifeBalance"].id,
                   title="#WorkLifeBalance How can employees request flexible working?",
                   content="Has anyone successfully requested hybrid working in their team? Would love some tips on the process.",
                   created_at=now - timedelta(days=4))
    p2 = ForumPost(author_id=people[5][0].id, category_id=cats["CareerGrowth"].id,
                   title="Which cloud certification is worth doing first?",
                   content="Planning to move into platform engineering. Looking for advice from people who made the switch internally.",
                   created_at=now - timedelta(days=3))
    p3 = ForumPost(author_id=people[9][0].id, category_id=cats["AnonymousFeedback"].id, is_anonymous=True,
                   title="Overtime has become the norm in some teams",
                   content="Several of us regularly work late during release weeks. Could workload planning be reviewed?",
                   created_at=now - timedelta(days=1))
    db.session.add_all([ann, p1, p2, p3])
    db.session.flush()
    db.session.add(ForumComment(post_id=p1.id, author_id=hr.id, is_hr_response=True, created_at=now - timedelta(days=3),
                                content="Great question! Raise a request via your manager and HR will review it within 3 working days."))
    db.session.add(ForumComment(post_id=p2.id, author_id=demo.id, created_at=now - timedelta(days=2),
                                content="I started with the fundamentals-level cert, then went for the associate exam."))
    db.session.add(ForumComment(post_id=p3.id, author_id=hr.id, is_hr_response=True, created_at=now - timedelta(hours=12),
                                content="Thank you for raising this. We are reviewing workload data for release weeks."))
    for u, _ in people[:9]:
        db.session.add(ForumLike(post_id=p1.id, user_id=u.id))
    for u, _ in people[:5]:
        db.session.add(ForumLike(post_id=p3.id, user_id=u.id))

    # ---- notifications
    for msg, link, kind, ago in [
            ("Interview scheduled for 'Senior Cloud Engineer' tomorrow at 11:00.", "/employee/interviews", "interview", 3),
            ("You have received an offer for 'React Frontend Developer'.", "/employee/offers", "offer", 40),
            ("Your application for 'Data Platform Engineer' has been shortlisted.", "/employee/applications", "application", 30),
            ("HR responded to your forum post.", "/forum", "forum", 70),
            ("New internal opportunity: DevOps Lead", "/employee/opportunities", "application", 120)]:
        db.session.add(Notification(user_id=demo.id, message=msg, link=link, kind=kind, created_at=now - timedelta(minutes=ago)))
    hi = sum(1 for r in results if r["risk_level"] == "High")
    db.session.add(Notification(user_id=hr.id, message=f"Weekly AI scan complete: {hi} high-risk employees detected.", link="/hr/", kind="risk", created_at=now - timedelta(minutes=15)))
    db.session.add(Notification(user_id=hr.id, message="Priya Sharma accepted interview slot for 'Senior Cloud Engineer'.", link="/recruitment/interviews", kind="interview", created_at=now - timedelta(minutes=50)))
    db.session.add(Notification(user_id=mgr.id, message="High attrition risk flagged for a member of your team.", link="/manager/", kind="risk", created_at=now - timedelta(hours=1)))
    db.session.add(Notification(user_id=ceo.id, message="Quarterly attrition overview is ready.", link="/ceo/", kind="info", created_at=now - timedelta(hours=3)))

    for act, cat, mins in [("Signed in", "auth", 600), ("Ran AI prediction batch for all employees", "ai", 580),
                           ("Posted opportunity 'DevOps Lead'", "hiring", 500), ("Posted forum announcement", "forum", 300)]:
        db.session.add(ActivityLog(user_id=hr.id, action=act, category=cat, ip_address="127.0.0.1", timestamp=now - timedelta(minutes=mins)))
    db.session.commit()

    print("=" * 62)
    print(" RetainIQ demo data created —", len(emps), "employees scored by the ML model")
    print("=" * 62)
    for r, e, p in [("Administrator", "admin", "Admin@123"), ("CEO", "ceo", "Ceo@12345"), ("Tech Head", "techhead", "Tech@1234"),
                    ("HR", "hr", "Hr@12345"), ("Manager", "manager", "Manager@123"), ("Recruiter", "recruiter", "Recruit@123"),
                    ("Employee", "employee", "Employee@123")]:
        print(f"  {r:14s} {e}@{DOMAIN:12s} / {p}")
    print("  Staff sign-up code (for non-employee self-registration): see config.py")
    print("=" * 62)


if __name__ == "__main__":
    from app import create_app
    with create_app().app_context():
        run_seed(reset=True)
