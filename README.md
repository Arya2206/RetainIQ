# RetainIQ — AI Attrition & Retention Predictor

A role-based HR platform (Flask + scikit-learn) that predicts which employees may leave,
explains why, recommends retention actions, and lets HR test interventions before applying them.

## Run it

```bash
pip install -r requirements.txt
python app.py
```
Open **http://127.0.0.1:5000**. The first start creates the database, the demo data and trains the model
(takes a few seconds to a minute). Nothing else to run.

## Demo accounts
| Role | E-mail | Password |
|---|---|---|
| Administrator | admin@retainiq.com | Admin@123 |
| CEO | ceo@retainiq.com | Ceo@12345 |
| Tech Head | techhead@retainiq.com | Tech@1234 |
| HR | hr@retainiq.com | Hr@12345 |
| Manager | manager@retainiq.com | Manager@123 |
| Recruiter | recruiter@retainiq.com | Recruit@123 |
| Employee | employee@retainiq.com | Employee@123 |

New employees can sign up on the login page. Other roles need the staff access code
(`STAFF_ACCESS_CODE` in `config.py`, default `RETAINIQ2026`).

## Highlights
- **AI:** 3 models compared by 5-fold CV, probability calibration, economics-based flagging threshold, per-employee risk factors, What-If simulator.
- **Responsible AI:** Age is *not* a model input; a fairness audit, calibration chart, lift table and drift (PSI) check on every upload are built in.
- **HR dashboard:** filters, date range, attrition trend, department risk, expected attrition cost, AI HR Assistant.
- **Excel pipeline:** validate -> clean -> predict -> data-quality report (1,000 rows in ~1.3 s).
- **Security:** hashed passwords, CSRF tokens, account lock-out, security headers, audit log, two-layer authorisation
  (role = area, permission = action) with an editable permission matrix and row-level scoping.
- **Workflows:** internal hiring (apply -> review -> shortlist -> interview -> offer), forum with HR announcements, notifications.
- **Works offline:** Bootstrap, icons and Chart.js are bundled in `static/vendor`; dark mode included.

## Tests
```bash
python -m unittest discover -s tests -t .
```
26 automated tests (auth, RBAC, AI, Excel pipeline, hiring, forum, admin).

## Database
SQLite by default. For MySQL: `pip install pymysql` and set
`DATABASE_URL=mysql+pymysql://user:password@localhost/retainiq` before starting (not covered by the automated tests).

## Note on data
The model is trained on a **synthetic** dataset (`ml/train_model.py`). Replace `generate_synthetic_dataset()` with a real CSV
having the same feature columns (see `FEATURES` in `ml/train_model.py`; `Age` is kept only for the fairness audit) to train on real data.

Reset demo data: `python seed.py`
