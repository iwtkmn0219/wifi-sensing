r"""
재실 판별 모델 학습·평가 — features.py가 만든 창 특징 표로 "빈 방(empty) vs 정적 재실(static)"을 학습한다.

사용 예
    cd E:\esp\wifi-sensing\pc
    python features.py --win 30 --step 10      # 먼저 특징 표
    python train.py                            # ..\processed\features_win30s.csv, RX COM4
    python train.py --features ..\processed\features_win60s.csv --rx COM4 --test-frac 0.3

테스트 데이터 (실측에서 떼어냄, 합성 데이터 아님)
    세션마다 시간순으로 앞 (1 − test_frac)을 학습, 뒤 test_frac을 테스트로 쓴다.
    창이 겹치므로 경계 앞뒤로 창 길이만큼 걸친 창은 버린다 (같은 측정 순간이 학습·테스트에 동시에 들어가지 않게).
    → ..\processed\split_<task>_train.csv / _test.csv 로 저장 (어떤 창이 테스트인지 그대로 확인 가능)

한계 (2026-11-21 데이터)
    클래스마다 세션이 하나씩이라(빈 방 = 155026, 재실 = 163812) 테스트는 "같은 세션의 몇 분 뒤"를 맞히는지만 본다.
    클래스 차이와 세션(배치·환경·시간) 차이를 구분할 수 없어서, 점수가 높아도 새 세션·새 배치에서의 성능은 보장하지 않는다.
    그래서 특징 묶음별(호흡 주기 특징만 / 진폭 흔들림 특징만)로도 학습해서 어느 쪽이 점수를 만드는지 같이 보여준다.

출력
    화면: 데이터 분할, 모델별 정확도·균형 정확도·혼동행렬, 특징 중요도
    ..\processed\models\<task>_<model>.joblib  (scikit-learn 파이프라인 + 사용한 특징 목록)
"""
from __future__ import annotations

import argparse
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, balanced_accuracy_score, confusion_matrix
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from features import FEATURES

ROOT = Path(__file__).resolve().parent.parent / "processed"
FEATURE_SETS = {
    "전체": FEATURES,
    "호흡 주기만": ["periodicity", "peak_bpm", "breath_ratio", "breath_entropy", "acf_peak"],
    "진폭 흔들림만": ["breath_std", "motion_std", "slow_std", "amp_std", "pc1_var_ratio"],
}


def temporal_split(df: pd.DataFrame, test_frac: float,
                   test_sessions: list[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """세션별 시간순 분할. 경계에 걸친 창(겹침)은 버린다.
    test_sessions(접두어 일치)에 해당하는 세션은 통째로 테스트에 넣는다 → 처음 보는 세션에서의 성능."""
    tr, te = [], []
    test_sessions = test_sessions or []
    for sess, g in df.groupby("session"):
        if any(str(sess).startswith(s) for s in test_sessions):
            te.append(g)
            continue
        g = g.sort_values("t0")
        win = float(g["win"].iloc[0])
        t_start, t_end = g["t0"].min(), g["t0"].max() + win
        cut = t_start + (1 - test_frac) * (t_end - t_start)
        tr.append(g[g["t0"] + win <= cut])               # 창 전체가 경계 앞
        te.append(g[g["t0"] >= cut + win])               # 경계 뒤로 창 길이만큼 띄움
    return pd.concat(tr), pd.concat(te)


def models() -> dict:
    return {
        "다수 클래스(기준선)": DummyClassifier(strategy="most_frequent"),
        "로지스틱 회귀": make_pipeline(StandardScaler(), LogisticRegression(class_weight="balanced", max_iter=2000)),
        "랜덤 포레스트": RandomForestClassifier(n_estimators=300, min_samples_leaf=3, class_weight="balanced",
                                          random_state=0),
    }


def evaluate(train: pd.DataFrame, test: pd.DataFrame, feats: list[str], classes: list[str], verbose: bool):
    Xtr, ytr = train[feats].to_numpy(float), train["cls"].to_numpy()
    Xte, yte = test[feats].to_numpy(float), test["cls"].to_numpy()
    results, fitted = {}, {}
    for name, m in models().items():
        m.fit(Xtr, ytr)
        p = m.predict(Xte)
        results[name] = (accuracy_score(yte, p), balanced_accuracy_score(yte, p), confusion_matrix(yte, p, labels=classes))
        fitted[name] = m
        if verbose:
            cm = results[name][2]
            cm_txt = " / ".join(f"실제 {c}: " + ", ".join(f"{classes[j]} {cm[i, j]}" for j in range(len(classes)))
                                for i, c in enumerate(classes))
            print(f"  {name:<14} 정확도 {results[name][0]:.3f} | 균형 정확도 {results[name][1]:.3f} | {cm_txt}")
    return results, fitted


def main() -> None:
    ap = argparse.ArgumentParser(description="빈 방 vs 정적 재실 모델 학습·평가")
    ap.add_argument("--features", default=str(ROOT / "features_win30s.csv"))
    ap.add_argument("--rx", default="COM4", help="사용할 RX (빈 방 데이터가 있는 RX)")
    ap.add_argument("--classes", nargs=2, default=["empty", "static"])
    ap.add_argument("--test-frac", type=float, default=0.3)
    ap.add_argument("--test-sessions", nargs="*", default=[],
                    help="통째로 테스트에 넣을 세션 (학습에 전혀 안 씀, 접두어 일치). 예: 20261121_162338")
    a = ap.parse_args()

    df = pd.read_csv(a.features)
    df = df[(df["rx"] == a.rx) & df["cls"].isin(a.classes)].dropna(subset=FEATURES)
    task = "_vs_".join(a.classes)
    if df["cls"].nunique() < 2:
        raise SystemExit(f"{a.rx}에 {a.classes} 두 클래스가 모두 있어야 합니다")

    train, test = temporal_split(df, a.test_frac, a.test_sessions)
    for name, part in (("train", train), ("test", test)):
        part.to_csv(ROOT / f"split_{task}_{a.rx}_{name}.csv", index=False, encoding="utf-8-sig")

    print(f"과제: {a.classes[0]} vs {a.classes[1]} | RX {a.rx} | 특징 표 {Path(a.features).name} (창 {df['win'].iloc[0]:g}초)")
    print("세션별 클래스:", ", ".join(f"{s} → {'/'.join(sorted(g.cls.unique()))}" for s, g in df.groupby("session")))
    print("분할(세션마다 시간순, 경계 겹침 제거):")
    for name, part in (("학습", train), ("테스트", test)):
        print(f"  {name}: " + ", ".join(f"{c} {int((part.cls == c).sum())}창" for c in a.classes))
    if test["cls"].nunique() < 2 or train["cls"].nunique() < 2:
        raise SystemExit("학습 또는 테스트에 한 클래스만 있습니다. --test-frac 또는 창 길이를 조정하세요")

    if a.test_sessions:
        print(f"  (통째로 테스트: {', '.join(a.test_sessions)} — 학습에 전혀 쓰지 않은 세션)")
    print("\n[전체 특징] 테스트 결과")
    _, fitted = evaluate(train, test, FEATURES, a.classes, verbose=True)

    print("\n테스트 세션별 정답률 (그 세션 창 중 맞힌 비율)")
    for sess, g in test.groupby("session"):
        src = "통째로 테스트" if any(str(sess).startswith(s) for s in a.test_sessions) else "뒤 30%"
        accs = [f"{name} {np.mean(fitted[name].predict(g[FEATURES].to_numpy(float)) == g['cls'].to_numpy()):.2f}"
                for name in ("로지스틱 회귀", "랜덤 포레스트")]
        print(f"  {sess} ({'/'.join(sorted(g.cls.unique()))}, {len(g)}창, {src}): " + " | ".join(accs))

    rf = fitted["랜덤 포레스트"]
    imp = pd.Series(rf.feature_importances_, index=FEATURES).sort_values(ascending=False)
    print("\n랜덤 포레스트 특징 중요도:", ", ".join(f"{k} {v:.2f}" for k, v in imp.items()))

    print("\n[특징 묶음별] 균형 정확도 — 어느 쪽 특징이 점수를 만드는지 (세션 차이 점검)")
    for set_name, feats in FEATURE_SETS.items():
        res, _ = evaluate(train, test, feats, a.classes, verbose=False)
        print(f"  {set_name:<8} 로지스틱 {res['로지스틱 회귀'][1]:.3f} | 랜덤 포레스트 {res['랜덤 포레스트'][1]:.3f}")

    out = ROOT / "models"
    out.mkdir(parents=True, exist_ok=True)
    for name, key in (("로지스틱 회귀", "logreg"), ("랜덤 포레스트", "rf")):
        joblib.dump({"model": fitted[name], "features": FEATURES, "classes": a.classes, "rx": a.rx,
                     "source": str(a.features)}, out / f"{task}_{a.rx}_{key}.joblib")
    print(f"\n모델 저장: {out}\\{task}_{a.rx}_*.joblib | 테스트 창 목록: {ROOT}\\split_{task}_{a.rx}_test.csv")


if __name__ == "__main__":
    main()
