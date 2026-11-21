r"""
재실 판별 모델 결과 그림 (발표용) — train.py와 같은 분할·모델로 다시 계산해서 그린다.

사용
    cd E:\esp\wifi-sensing\pc
    python plot_results.py                      # ..\docs\figures\model_results.png

패널
  (a) 평가 방식별 성능: 같은 세션 뒤 30% (균형 정확도) vs 세션 통째로 테스트 (그 세션 정답률)
  (b) 특징 묶음별 균형 정확도: 전체 / 호흡 주기만 / 진폭 흔들림만 (같은 세션 뒤 30%)
  (c) 창 분포: 진폭 흔들림(amp_std) × 서브캐리어 공통 변화(pc1_var_ratio), 세션별 모양
  (d) 시간에 따른 재실 확률 (랜덤 포레스트, 같은 세션 뒤 30% 분할): 학습 구간 vs 테스트 구간
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import balanced_accuracy_score

from features import FEATURES
from train import FEATURE_SETS, ROOT, models, temporal_split

# 팔레트 (dataviz 기본, light): 클래스 = slot1 blue(빈 방) / slot2 orange(재실). 모델은 잉크 명암으로 구분
C_EMPTY, C_STATIC = "#2a78d6", "#eb6834"
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
C_LR, C_RF, C_BASE = "#52514e", "#a9a7a0", "#d6d4cd"
SESSION_NAME = {"20261121_155026": "빈 방 (15:50, 26분)",
                "20261121_162338": "재실 A (16:23, 12분)",
                "20261121_163812_breath": "재실 B (16:38, 11분)"}
MARKER = {"20261121_155026": "o", "20261121_162338": "^", "20261121_163812_breath": "s"}

plt.rcParams.update({
    "font.family": "Malgun Gothic", "axes.unicode_minus": False, "font.size": 9,
    "axes.edgecolor": GRID, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
    "axes.titlecolor": INK, "axes.titlesize": 10, "axes.titleweight": "bold", "axes.titlelocation": "left",
    "figure.facecolor": SURF, "axes.facecolor": SURF, "legend.frameon": False,
    "axes.spines.top": False, "axes.spines.right": False,
})


def fit(train, feats):
    out = {}
    for name, m in models().items():
        m.fit(train[feats].to_numpy(float), train["cls"].to_numpy())
        out[name] = m
    return out


def bal(m, test, feats):
    return balanced_accuracy_score(test["cls"], m.predict(test[feats].to_numpy(float)))


def recall(m, g, feats):
    return float(np.mean(m.predict(g[feats].to_numpy(float)) == g["cls"].to_numpy()))


def bar_labels(ax, bars, fmt="{:.2f}"):
    for b in bars:
        h = b.get_height()
        ax.text(b.get_x() + b.get_width() / 2, h + 0.015, fmt.format(h), ha="center", va="bottom",
                fontsize=8, color=INK)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default=str(ROOT / "features_win30s.csv"))
    ap.add_argument("--rx", default="COM4")
    ap.add_argument("--out", default=str(ROOT.parent / "docs" / "figures" / "model_results.png"))
    a = ap.parse_args()

    df = pd.read_csv(a.features)
    df = df[(df.rx == a.rx) & df.cls.isin(["empty", "static"])].dropna(subset=FEATURES)
    win = df["win"].iloc[0]

    # 같은 세션 뒤 30%
    tr, te = temporal_split(df, 0.3)
    m_all = fit(tr, FEATURES)
    # 세션 통째로 테스트
    held = {}
    for s in ("20261121_162338", "20261121_163812_breath"):
        tr2, te2 = temporal_split(df, 0.3, [s])
        mm = fit(tr2, FEATURES)
        g = te2[te2.session == s]
        held[s] = {n: recall(mm[n], g, FEATURES) for n in ("로지스틱 회귀", "랜덤 포레스트")}

    fig, axes = plt.subplots(2, 2, figsize=(13, 8.6))
    fig.suptitle(f"빈 방 vs 정적 재실 판별 결과 — RX {a.rx}, {win:g}초 창, 2026-11-21 데이터",
                 fontsize=12, fontweight="bold", color=INK, x=0.01, ha="left")

    # (a) 평가 방식별
    ax = axes[0, 0]
    groups = ["같은 세션 뒤 30%\n(균형 정확도)", "재실 A 통째로 테스트\n(그 세션 정답률)", "재실 B 통째로 테스트\n(그 세션 정답률)"]
    lr = [bal(m_all["로지스틱 회귀"], te, FEATURES), held["20261121_162338"]["로지스틱 회귀"],
          held["20261121_163812_breath"]["로지스틱 회귀"]]
    rf = [bal(m_all["랜덤 포레스트"], te, FEATURES), held["20261121_162338"]["랜덤 포레스트"],
          held["20261121_163812_breath"]["랜덤 포레스트"]]
    x = np.arange(3)
    b1 = ax.bar(x - 0.19, lr, 0.36, color=C_LR, label="로지스틱 회귀")
    b2 = ax.bar(x + 0.19, rf, 0.36, color=C_RF, label="랜덤 포레스트", edgecolor=SURF, linewidth=2)
    bar_labels(ax, b1); bar_labels(ax, b2)
    ax.axhline(0.5, color=INK2, lw=1, ls=(0, (3, 2)), label="우연 수준 0.5")
    ax.set_xticks(x, groups)
    ax.set_ylim(0, 1.12)
    ax.set_ylabel("점수")
    ax.set_title("(a) 같은 세션 안에서는 높지만, 처음 보는 세션에서는 흔들린다")
    ax.legend(loc="upper right", fontsize=8, ncol=3, bbox_to_anchor=(1, 1.03))
    ax.grid(axis="y", color=GRID, lw=0.6)

    # (b) 특징 묶음별
    ax = axes[0, 1]
    names = list(FEATURE_SETS)
    lr_b, rf_b = [], []
    for nm in names:
        mm = fit(tr, FEATURE_SETS[nm])
        lr_b.append(bal(mm["로지스틱 회귀"], te, FEATURE_SETS[nm]))
        rf_b.append(bal(mm["랜덤 포레스트"], te, FEATURE_SETS[nm]))
    x = np.arange(len(names))
    b1 = ax.bar(x - 0.19, lr_b, 0.36, color=C_LR, label="로지스틱 회귀")
    b2 = ax.bar(x + 0.19, rf_b, 0.36, color=C_RF, label="랜덤 포레스트", edgecolor=SURF, linewidth=2)
    bar_labels(ax, b1); bar_labels(ax, b2)
    ax.axhline(0.5, color=INK2, lw=1, ls=(0, (3, 2)), label="우연 수준 0.5")
    ax.set_xticks(x, ["전체 특징 10개", "호흡 주기 특징만\n(주기성·호흡수 등 5개)", "진폭 흔들림 특징만\n(대역별 표준편차 등 5개)"])
    ax.set_ylim(0, 1.12)
    ax.set_ylabel("균형 정확도 (같은 세션 뒤 30%)")
    ax.set_title("(b) 구분은 주로 진폭 흔들림에서 나온다 (호흡 특징은 우연보다 약간 위)")
    ax.legend(loc="upper left", fontsize=8, ncol=3)
    ax.grid(axis="y", color=GRID, lw=0.6)

    # (c) 창 분포
    ax = axes[1, 0]
    for s, g in df.groupby("session"):
        color = C_EMPTY if (g.cls == "empty").all() else C_STATIC
        ax.scatter(g.amp_std, g.pc1_var_ratio, s=22, marker=MARKER.get(s, "o"), color=color, alpha=0.8,
                   edgecolors=SURF, linewidths=0.6, label=SESSION_NAME.get(s, s))
    lo, hi = np.percentile(df.amp_std, [0.5, 99.5])
    n_out = int(((df.amp_std < lo) | (df.amp_std > hi)).sum())
    ax.set_xlim(lo - 0.3, hi + 0.3)
    if n_out:
        ax.text(0.99, 0.02, f"범위 밖 창 {n_out}개는 생략", transform=ax.transAxes, ha="right", fontsize=7.5, color=INK2)
    ax.set_xlabel("진폭 흔들림 amp_std (서브캐리어별 표준편차 중앙값 ×1e3)")
    ax.set_ylabel("서브캐리어 공통 변화 pc1_var_ratio")
    ax.set_title("(c) 창 하나 = 점 하나: 재실 A는 빈 방 쪽과 일부 겹친다")
    ax.legend(fontsize=8, loc="upper right")
    ax.grid(color=GRID, lw=0.6)

    # (d) 시간에 따른 재실 확률
    ax = axes[1, 1]
    rfm = m_all["랜덤 포레스트"]
    classes = list(rfm.classes_)
    offset = 0.0
    ticks, tick_labels = [], []
    test_keys = set(zip(te.session, te.t0))
    for s, g in df.sort_values("t0").groupby("session", sort=False):
        g = g.sort_values("t0")
        p = rfm.predict_proba(g[FEATURES].to_numpy(float))[:, classes.index("static")]
        tmin = (g.t0 - g.t0.min()).to_numpy() / 60 + offset
        is_test = np.array([(s, t) in test_keys for t in g.t0])
        color = C_EMPTY if (g.cls == "empty").all() else C_STATIC
        if is_test.any():
            ax.axvspan(tmin[is_test].min(), tmin[is_test].max() + win / 60, color=GRID, alpha=0.7, lw=0)
        ax.plot(tmin, p, color=color, lw=1.6, marker=MARKER.get(s, "o"), ms=3.5)
        ticks.append((tmin.min() + tmin.max()) / 2)
        tick_labels.append(SESSION_NAME.get(s, s))
        offset = tmin.max() + 3
    ax.axhline(0.5, color=INK2, lw=1, ls=(0, (3, 2)))
    ax.set_xticks(ticks, tick_labels)
    ax.set_ylim(-0.03, 1.08)
    ax.set_ylabel("랜덤 포레스트 재실 확률")
    ax.set_title("(d) 세션별 재실 확률 (회색 음영 = 테스트 구간, 나머지는 학습 구간)")
    ax.grid(axis="y", color=GRID, lw=0.6)
    ax.text(0.99, 0.02, "빈 방은 0, 재실은 1에 가까워야 정답", transform=ax.transAxes, ha="right", fontsize=8, color=INK2)

    fig.text(0.01, 0.005,
             f"테스트는 실측에서 떼어냄(합성 아님): 세션마다 시간순 뒤 30%, 경계에 걸친 창 제외. '통째로 테스트'는 그 세션을 학습에 전혀 쓰지 않음. "
             f"빈 방 세션이 하나뿐이라 처음 보는 빈 방에 대한 성능은 검증하지 못함.",
             fontsize=7.5, color=INK2)
    fig.tight_layout(rect=(0, 0.02, 1, 0.96))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=130)
    print("저장:", out)


if __name__ == "__main__":
    main()
