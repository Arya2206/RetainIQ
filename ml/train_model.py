"""
Trains the RetainIQ attrition model.

Pipeline
    synthetic HR dataset (correlated, realistic) -> 3 candidate algorithms -> 5-fold CV
    -> probability calibration -> economics-based decision threshold -> hold-out evaluation
    -> fairness audit -> drift reference -> bundle saved to disk.

Design decisions that matter in the real world
    * Age is NOT a model input. It is a protected characteristic, so it is only kept
      in the dataset to audit the model for age bias (see `fairness_audit`).
    * Probabilities are calibrated, so "60" on the dashboard really means ~60 in 100
      similar employees leave. Raw tree-ensemble scores are not probabilities.
    * Accuracy is misleading on imbalanced data (a model that says "nobody leaves"
      is ~80% accurate), so we also report PR-AUC, Brier score and lift@top-k.
    * The flagging threshold comes from retention economics, not from the default 0.5.

Run:  python ml/train_model.py
"""
import json
import os
import platform
from datetime import datetime, timezone

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.calibration import CalibratedClassifierCV, calibration_curve
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.inspection import permutation_importance
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, average_precision_score, brier_score_loss,
                             confusion_matrix, f1_score, precision_score, recall_score,
                             roc_auc_score, roc_curve)
from sklearn.model_selection import StratifiedKFold, cross_validate, train_test_split
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

MODEL_VERSION = "3.0"

# Age is intentionally absent: it is a protected attribute (see module docstring).
FEATURES = [
    "MonthlySalary", "YearsAtCompany", "JobSatisfaction", "WorkLifeBalance",
    "OverTime", "PerformanceRating", "YearsSincePromotion", "TrainingTimesLastYear",
    "DistanceFromHome", "NumCompaniesWorked", "SalaryHikePercent",
]
PROTECTED = ["Age"]          # stored for fairness auditing only
FEATURE_LABELS = {
    "Age": "Age", "MonthlySalary": "Monthly salary", "YearsAtCompany": "Tenure",
    "JobSatisfaction": "Job satisfaction", "WorkLifeBalance": "Work-life balance",
    "OverTime": "Overtime", "PerformanceRating": "Performance rating",
    "YearsSincePromotion": "Years since promotion", "TrainingTimesLastYear": "Training sessions",
    "DistanceFromHome": "Commute distance", "NumCompaniesWorked": "Previous employers",
    "SalaryHikePercent": "Last salary hike",
}

# ---- retention economics (all expressed relative to the cost of replacing one employee = 1.0)
INTERVENTION_COST = 0.12      # a retention conversation + action costs ~12% of a replacement
INTERVENTION_SUCCESS = 0.40   # share of at-risk leavers an intervention actually keeps
# Intervene when  p * success * 1.0  >  cost   <=>   p > cost / success   (= 0.30)
DECISION_THRESHOLD = round(INTERVENTION_COST / INTERVENTION_SUCCESS, 4)
HIGH_THRESHOLD = 0.60

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "attrition_model.pkl")
REPORT_PATH = os.path.join(HERE, "model_report.json")


# --------------------------------------------------------------------------- data
def generate_synthetic_dataset(n=8000, seed=42):
    """Synthetic HR data with realistic structure.

    Unlike independent random columns, features here are correlated the way they are in
    real companies: tenure is bounded by age, pay grows with tenure and performance,
    raises follow performance, promotion gaps cannot exceed tenure, and a hidden
    "engagement" factor links satisfaction, work-life balance and overtime. The attrition
    label follows well-documented drivers plus noise. Swap in a real CSV with the same
    columns (plus Attrition) to train on company data."""
    rng = np.random.default_rng(seed)
    age = np.clip(rng.normal(36, 8.5, n), 21, 60).round()
    tenure = np.clip(rng.gamma(2.0, 2.4, n), 0, age - 20.5)
    perf = np.clip(np.round(rng.normal(3.2, 0.9, n)), 1, 5)
    engagement = rng.normal(0, 1, n)                                     # hidden factor
    js = np.clip(np.round(2.6 + 0.75 * engagement + rng.normal(0, 0.7, n)), 1, 4)
    wlb = np.clip(np.round(2.7 + 0.55 * engagement + rng.normal(0, 0.8, n)), 1, 4)
    ot = (rng.random(n) < 1 / (1 + np.exp(1.0 + 0.7 * engagement + 0.25 * (wlb - 2.7)))).astype(int)
    salary = np.clip(rng.lognormal(10.55, 0.38, n) * (1 + 0.035 * tenure) * (1 + 0.06 * (perf - 3)),
                     12000, 250000)
    hike = np.clip(8 + 2.2 * (perf - 3) + rng.normal(0, 3.2, n) + 0.4 * engagement, 0, 30)
    ysp = np.clip(rng.exponential(1.8, n) + 0.12 * tenure, 0, np.maximum(tenure, 0.0))
    train = np.clip(rng.poisson(2.8, n) + (perf >= 4).astype(int), 0, 10)
    dist = np.clip(rng.lognormal(2.25, 0.7, n), 1, 60)
    ncw = np.clip(rng.poisson(1.2 + 0.07 * (age - 21), n), 0, 9)

    z = (-0.00002 * (salary - 55000) + 0.60 * (3 - js) + 0.45 * (3 - wlb) + 1.0 * ot
         + 0.28 * ysp - 0.16 * train + 0.025 * dist + 0.14 * ncw - 0.12 * hike
         - 0.06 * tenure - 0.14 * (perf - 3)
         + 0.35 * np.exp(-tenure / 1.2)                                    # first-year churn
         + rng.normal(0, 0.7, n))                                          # unobserved reasons
    thr = np.quantile(z, 0.83)
    prob = 1 / (1 + np.exp(-(z - thr) * 1.5))
    y = (rng.random(n) < prob).astype(int)

    return pd.DataFrame({
        "Age": age.astype(int), "MonthlySalary": salary.round(0), "YearsAtCompany": tenure.round(1),
        "JobSatisfaction": js.astype(int), "WorkLifeBalance": wlb.astype(int), "OverTime": ot,
        "PerformanceRating": perf.astype(int), "YearsSincePromotion": ysp.round(1),
        "TrainingTimesLastYear": train.astype(int), "DistanceFromHome": dist.round(1),
        "NumCompaniesWorked": ncw.astype(int), "SalaryHikePercent": hike.round(1), "Attrition": y,
    })


# --------------------------------------------------------------------------- evaluation helpers
def _binary_metrics(y_true, prob, threshold):
    pred = (prob >= threshold).astype(int)
    return {
        "accuracy": round(accuracy_score(y_true, pred), 4),
        "precision": round(precision_score(y_true, pred, zero_division=0), 4),
        "recall": round(recall_score(y_true, pred, zero_division=0), 4),
        "f1": round(f1_score(y_true, pred, zero_division=0), 4),
    }


def lift_table(y_true, prob, fractions=(0.05, 0.10, 0.20, 0.30)):
    """If HR can only talk to the top k% riskiest employees, how many real leavers do they reach?"""
    y_true = np.asarray(y_true)
    order = np.argsort(-prob)
    base = y_true.mean()
    rows = []
    for f in fractions:
        k = max(1, int(round(len(y_true) * f)))
        hit = y_true[order[:k]]
        rows.append({"top_pct": int(f * 100), "precision": round(float(hit.mean()), 4),
                     "recall": round(float(hit.sum() / max(y_true.sum(), 1)), 4),
                     "lift": round(float(hit.mean() / base), 2)})
    return rows


def economics(y_true, prob, threshold):
    """Net value (in 'replacements') of acting on the model vs doing nothing, per 100 employees."""
    y_true = np.asarray(y_true)
    flagged = prob >= threshold
    saved = INTERVENTION_SUCCESS * y_true[flagged].sum()
    spent = INTERVENTION_COST * flagged.sum()
    n = len(y_true)
    return {"flagged_pct": round(float(100 * flagged.mean()), 1),
            "net_value_per_100": round(float(100 * (saved - spent) / n), 2),
            "leavers_per_100": round(float(100 * y_true.sum() / n), 1),
            "assumptions": {"intervention_cost": INTERVENTION_COST, "success_rate": INTERVENTION_SUCCESS}}


def fairness_audit(age, y_true, prob, threshold):
    """Does the model flag some age groups far more often than others (four-fifths rule)?
    Age is not a model input, so any gap comes from correlated features such as tenure."""
    bands = pd.cut(pd.Series(age), [0, 29, 39, 49, 100], labels=["<30", "30-39", "40-49", "50+"])
    y_true = np.asarray(y_true)
    flagged = prob >= threshold
    rows = []
    for b in bands.cat.categories:
        m = (bands == b).to_numpy()
        if m.sum() < 50:
            continue
        pos = y_true[m] == 1
        rows.append({"group": str(b), "n": int(m.sum()),
                     "flag_rate": round(float(flagged[m].mean()), 4),
                     "actual_rate": round(float(y_true[m].mean()), 4),
                     "recall": round(float(flagged[m][pos].mean()), 4) if pos.sum() else None,
                     "avg_risk": round(float(prob[m].mean() * 100), 1)})
    rates = [r["flag_rate"] for r in rows if r["flag_rate"] > 0]
    ratio = round(min(rates) / max(rates), 2) if rates else 1.0
    return {"attribute": "Age band", "groups": rows, "min_max_flag_ratio": ratio,
            "passes_four_fifths": bool(ratio >= 0.8),
            "note": "Age is excluded from the model; this audit checks for indirect bias."}


def drift_reference(X):
    """Decile edges + bin shares per feature, used to measure data drift (PSI) on new uploads."""
    ref = {}
    for f in FEATURES:
        v = X[f].to_numpy(dtype=float)
        edges = np.unique(np.quantile(v, np.linspace(0, 1, 11)))
        if len(edges) < 3:
            edges = np.array([v.min() - 1, v.mean(), v.max() + 1])
        inner = edges[1:-1]
        shares = np.bincount(np.searchsorted(inner, v, side="right"), minlength=len(inner) + 1) / len(v)
        ref[f] = {"edges": [float(e) for e in inner], "shares": [float(s) for s in shares]}
    return ref


# --------------------------------------------------------------------------- training
def _candidates():
    return {
        "Logistic Regression": make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000, C=0.5)),
        "Random Forest": RandomForestClassifier(n_estimators=250, max_depth=8, min_samples_leaf=10,
                                                random_state=42, n_jobs=1),
        "Gradient Boosting": GradientBoostingClassifier(n_estimators=160, max_depth=3, learning_rate=0.06,
                                                        subsample=0.8, random_state=42),
    }


def train_and_save(verbose=True):
    df = generate_synthetic_dataset()
    X, y = df[FEATURES], df["Attrition"]
    age = df["Age"]
    X_tr, X_te, y_tr, y_te, age_tr, age_te = train_test_split(
        X, y, age, test_size=0.2, random_state=42, stratify=y)

    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    comparison = []
    for name, est in _candidates().items():
        cv = cross_validate(est, X_tr, y_tr, cv=skf, n_jobs=1,
                            scoring={"auc": "roc_auc", "ap": "average_precision", "brier": "neg_brier_score"})
        comparison.append({
            "model": name,
            "cv_auc_mean": round(float(cv["test_auc"].mean()), 4), "cv_auc_std": round(float(cv["test_auc"].std()), 4),
            "cv_pr_auc": round(float(cv["test_ap"].mean()), 4),
            "cv_brier": round(float(-cv["test_brier"].mean()), 4),
        })
        if verbose:
            print(f"  {name:20s} CV-AUC {comparison[-1]['cv_auc_mean']:.3f}  CV-PR-AUC {comparison[-1]['cv_pr_auc']:.3f}")

    best_name = max(comparison, key=lambda c: c["cv_auc_mean"])["model"]
    # Calibrated final model: Platt (sigmoid) scaling on 5 internal folds turns raw scores into real probabilities.
    model = CalibratedClassifierCV(_candidates()[best_name], method="sigmoid", cv=5)
    model.fit(X_tr, y_tr)
    if hasattr(model, "n_jobs"):
        model.n_jobs = 1

    # Hold-out metrics for every candidate (calibrated the same way, so the table is comparable).
    for row in comparison:
        m = model if row["model"] == best_name else CalibratedClassifierCV(
            _candidates()[row["model"]], method="sigmoid", cv=5).fit(X_tr, y_tr)
        p = m.predict_proba(X_te)[:, 1]
        row.update(_binary_metrics(y_te, p, DECISION_THRESHOLD))
        row.update({"roc_auc": round(roc_auc_score(y_te, p), 4),
                    "pr_auc": round(average_precision_score(y_te, p), 4),
                    "brier": round(brier_score_loss(y_te, p), 4)})

    probs = model.predict_proba(X_te)[:, 1]
    best = next(c for c in comparison if c["model"] == best_name)
    tn, fp, fn, tp = confusion_matrix(y_te, (probs >= DECISION_THRESHOLD).astype(int)).ravel()
    fpr, tpr, _ = roc_curve(y_te, probs)
    idx = np.linspace(0, len(fpr) - 1, 40).astype(int)
    frac_pos, mean_pred = calibration_curve(y_te, probs, n_bins=8, strategy="quantile")

    perm = permutation_importance(model, X_te, y_te, n_repeats=5, random_state=42, scoring="roc_auc", n_jobs=1)
    importances = {f: round(float(v), 5) for f, v in zip(FEATURES, perm.importances_mean)}

    baseline_acc = round(1 - float(y_te.mean()), 4)       # accuracy of "nobody ever leaves"
    metrics = {
        "model_name": best_name, "model_version": MODEL_VERSION,
        "accuracy": best["accuracy"], "precision": best["precision"], "recall": best["recall"],
        "f1": best["f1"], "roc_auc": best["roc_auc"], "pr_auc": best["pr_auc"], "brier": best["brier"],
        "cv_auc_mean": best["cv_auc_mean"], "cv_auc_std": best["cv_auc_std"],
        "majority_baseline_accuracy": baseline_acc,
        "decision_threshold": DECISION_THRESHOLD, "high_threshold": HIGH_THRESHOLD,
        "confusion_matrix": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
        "roc_curve": {"fpr": [round(float(fpr[i]), 4) for i in idx], "tpr": [round(float(tpr[i]), 4) for i in idx]},
        "calibration": {"predicted": [round(float(v), 4) for v in mean_pred],
                        "observed": [round(float(v), 4) for v in frac_pos]},
        "lift": lift_table(y_te, probs),
        "economics": economics(y_te, probs, DECISION_THRESHOLD),
        "fairness": fairness_audit(age_te.to_numpy(), y_te, probs, DECISION_THRESHOLD),
        "comparison": comparison, "n_train": int(len(X_tr)), "n_test": int(len(X_te)),
        "positive_rate": round(float(y.mean()), 4), "features": FEATURES, "excluded_features": PROTECTED,
        "trained_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "environment": {"scikit_learn": sklearn.__version__, "python": platform.python_version()},
    }
    bundle = {
        "model": model, "features": FEATURES, "version": MODEL_VERSION,
        "baselines": {f: float(X[f].median()) for f in FEATURES},
        "feature_importances": importances, "metrics": metrics,
        "drift_reference": drift_reference(X_tr),
        "thresholds": {"decision": DECISION_THRESHOLD, "high": HIGH_THRESHOLD},
    }
    joblib.dump(bundle, MODEL_PATH)
    with open(REPORT_PATH, "w") as fh:
        json.dump({"metrics": metrics, "feature_importances": importances}, fh, indent=2)
    if verbose:
        print(f"Selected: {best_name} | AUC {metrics['roc_auc']} | PR-AUC {metrics['pr_auc']} | "
              f"Brier {metrics['brier']} | recall {metrics['recall']} | saved {MODEL_PATH}")
    return bundle


if __name__ == "__main__":
    train_and_save()
