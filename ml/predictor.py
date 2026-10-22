"""Prediction service: vectorised scoring, per-employee explanations
(occlusion / counterfactual analysis against a 'typical employee' baseline),
human-readable risk factors, retention actions and What-If simulation."""
import os
import joblib
import numpy as np
import pandas as pd

from .train_model import (DECISION_THRESHOLD, FEATURES, FEATURE_LABELS, HIGH_THRESHOLD as _HIGH,
                          MODEL_PATH, MODEL_VERSION, train_and_save)

# Risk bands on the 0-100 scale. The Medium cut-off is the economic break-even point
# (intervene when expected savings exceed the cost of the intervention); see train_model.py.
HIGH_THRESHOLD = int(round(_HIGH * 100))
MEDIUM_THRESHOLD = int(round(DECISION_THRESHOLD * 100))

_bundle = None


def _load():
    """Load the saved model; retrain automatically if it is missing, was produced by an
    incompatible scikit-learn version, or was trained on a different feature schema."""
    global _bundle
    if _bundle is None:
        if os.path.exists(MODEL_PATH):
            try:
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("error")
                    _bundle = joblib.load(MODEL_PATH)
                if _bundle.get("features") != FEATURES or _bundle.get("version") != MODEL_VERSION:
                    _bundle = None          # stale model from an older release
            except Exception:
                _bundle = None
        if _bundle is None:
            print("Training the attrition model for this machine (one-time, ~30 seconds)...")
            _bundle = train_and_save(verbose=True)
        if hasattr(_bundle["model"], "n_jobs"):
            _bundle["model"].n_jobs = 1
    return _bundle


def risk_level(score):
    return "High" if score >= HIGH_THRESHOLD else ("Medium" if score >= MEDIUM_THRESHOLD else "Low")


# feature -> function(value, baseline) returning (factor label, recommended action) or None
def _describe(feature, v, base):
    rules = {
        "JobSatisfaction": (v <= 2, "Low job satisfaction", "Schedule a 1-to-1 discussion to understand concerns"),
        "OverTime": (v >= 1, "Frequent overtime", "Review workload and redistribute tasks"),
        "WorkLifeBalance": (v <= 2, "Poor work-life balance", "Discuss flexible working arrangements"),
        "SalaryHikePercent": (v < base, "Low salary growth", "Consider a compensation review"),
        "MonthlySalary": (v < base, "Below-median salary", "Benchmark pay against the market"),
        "YearsSincePromotion": (v >= 3, "Limited promotion opportunities", "Discuss career progression and promotion path"),
        "DistanceFromHome": (v >= 20, "Long commute", "Explore remote or hybrid working options"),
        "TrainingTimesLastYear": (v <= 2, "Low investment in training", "Enrol in relevant upskilling programmes"),
        "NumCompaniesWorked": (v >= 4, "History of frequent job changes", "Strengthen engagement and long-term incentives"),
        "YearsAtCompany": (v <= 1.5, "Early-tenure employee", "Assign a mentor and strengthen onboarding"),
        "PerformanceRating": (v <= 2, "Low performance rating", "Agree a support and development plan"),
    }
    hit = rules.get(feature)
    return (hit[1], hit[2]) if hit and hit[0] else None


def drift_report(feature_dicts):
    """Population Stability Index per feature: how different is this batch of employees from
    the data the model was trained on?  PSI < 0.10 stable, 0.10-0.25 moderate, > 0.25 major shift.
    Used on every Excel upload so HR knows when predictions should be treated with caution."""
    b = _load()
    ref = b.get("drift_reference")
    if not ref or len(feature_dicts) < 30:
        return {"available": False, "reason": "At least 30 employees are needed for a drift check.", "features": []}
    X = _frame(feature_dicts)
    rows = []
    for f in FEATURES:
        edges = np.array(ref[f]["edges"])
        expected = np.clip(np.array(ref[f]["shares"]), 1e-4, None)
        actual = np.bincount(np.searchsorted(edges, X[f].to_numpy(dtype=float), side="right"),
                             minlength=len(expected)) / len(X)
        actual = np.clip(actual, 1e-4, None)
        psi = float(np.sum((actual - expected) * np.log(actual / expected)))
        rows.append({"feature": FEATURE_LABELS[f], "psi": round(psi, 3),
                     "status": "major" if psi > 0.25 else ("moderate" if psi > 0.10 else "stable")})
    rows.sort(key=lambda r: -r["psi"])
    worst = rows[0]["status"] if rows else "stable"
    overall = "major" if any(r["status"] == "major" for r in rows) else (
        "moderate" if any(r["status"] == "moderate" for r in rows) else "stable")
    return {"available": True, "overall": overall, "features": rows, "worst": worst,
            "message": {"stable": "This workforce looks like the data the model was trained on.",
                        "moderate": "Some features differ from the training data; treat scores as indicative.",
                        "major": "This workforce differs substantially from the training data; "
                                 "consider retraining the model on your own history before acting on scores."}[overall]}


def _frame(feature_dicts):
    rows = [{f: float(d.get(f) or 0) for f in FEATURES} for d in feature_dicts]
    return pd.DataFrame(rows, columns=FEATURES)


def predict_many(feature_dicts, explain=True):
    """Score a list of feature dicts in one vectorised call."""
    if not feature_dicts:
        return []
    b = _load()
    model, base = b["model"], b["baselines"]
    X = _frame(feature_dicts)
    probs = model.predict_proba(X)[:, 1]

    def _logit(p):
        p = np.clip(p, 1e-4, 1 - 1e-4)
        return np.log(p / (1 - p))

    deltas = {}
    if explain:
        lg = _logit(probs)
        for f in FEATURES:
            Xm = X.copy()
            Xm[f] = base[f]       # replace with the 'typical employee' value
            deltas[f] = lg - _logit(model.predict_proba(Xm)[:, 1])   # + => feature raises risk
        pos_total = np.sum([np.clip(deltas[f], 0, None) for f in FEATURES], axis=0)

    out = []
    for i, d in enumerate(feature_dicts):
        p = float(probs[i])
        score = int(round(p * 100))
        level = risk_level(score)
        details, factors, actions = [], [], []
        if explain:
            ranked = sorted(FEATURES, key=lambda f: -deltas[f][i])
            for f in ranked:
                raw = float(deltas[f][i])
                if raw < 0.12 or pos_total[i] <= 0 or p < 0.05:
                    continue
                impact = 100.0 * raw / float(pos_total[i])    # share of total risk drivers (%)
                label_action = _describe(f, float(X.iloc[i][f]), base[f])
                if label_action:
                    details.append({"feature": FEATURE_LABELS[f], "label": label_action[0], "impact": round(impact, 1)})
                    factors.append(label_action[0])
                    if label_action[1] not in actions:
                        actions.append(label_action[1])
            if level in ("High", "Medium") and not any("1-to-1" in a for a in actions):
                actions.insert(0, "Schedule a 1-to-1 retention conversation with the manager")
            if not factors:
                factors = ["No significant risk drivers identified"]
                actions = ["Continue regular check-ins and recognition"]
        out.append({
            "probability": round(p, 4), "risk_score": score, "risk_level": level,
            "top_factors": factors[:5], "recommendations": actions[:5], "explanations": details[:6],
        })
    return out


def predict_employee(feature_dict, explain=True):
    return predict_many([feature_dict], explain)[0]


def what_if(feature_dict, changes):
    """Counterfactual: apply `changes` to the employee's features and re-score."""
    current = predict_employee(feature_dict)
    modified = dict(feature_dict)
    for k, v in changes.items():
        if k in FEATURES:
            modified[k] = float(v)
    new = predict_employee(modified)
    return {"current": current, "simulated": new,
            "delta": new["risk_score"] - current["risk_score"], "features": modified}


def model_info():
    b = _load()
    return {"metrics": b["metrics"], "feature_importances": b["feature_importances"],
            "feature_labels": FEATURE_LABELS, "baselines": b["baselines"]}


def model_metrics():
    b = _load()
    return b["metrics"], b["feature_importances"]
