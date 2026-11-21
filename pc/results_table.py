r"""
재실 판별 결과 요약 표 — train.py와 같은 분할·모델로 다시 계산해서 표(PNG·CSV·Markdown)로 만든다.

사용
    cd E:\esp\wifi-sensing\pc
    python results_table.py      # ..\docs\figures\results_table.png, ..\processed\results_table.csv, 화면에 Markdown 표

열
    같은 세션 뒤 30%      : 세션마다 시간순 앞 70% 학습 → 뒤 30% 테스트 (정확도, 균형 정확도)
    처음 보는 재실 세션   : 재실 세션 하나를 학습에서 통째로 빼고 평가한 균형 정확도 (A, B, 평균, 최저)
                            ★ 대표값 = 평균. 빈 방 쪽은 세션이 하나뿐이라 같은 세션 뒤 30%로 평가됨
    재실 놓침률           : 처음 보는 재실 세션 평가 2번에서 재실 창을 빈 방으로 판단한 비율 (낮을수록 좋음)
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, balanced_accuracy_score

from features import FEATURES
from train import MODEL_KEYS, ROOT, models, temporal_split

HELD = {"A": "20261121_162338", "B": "20261121_163812_breath"}
INK, INK2, GRID, SURF = "#0b0b0b", "#52514e", "#e4e3df", "#fcfcfb"
HL = "#fbe3d8"       # 대표값 열 강조 (slot2 orange의 옅은 단계 느낌, 글자는 잉크색)


def fit_predict(train, test):
    out = {}
    for name, m in models().items():
        m.fit(train[FEATURES].to_numpy(float), train["cls"].to_numpy())
        out[name] = m.predict(test[FEATURES].to_numpy(float))
    return out


def compute(features_csv: Path, rx: str = "COM4") -> pd.DataFrame:
    df = pd.read_csv(features_csv)
    df = df[(df.rx == rx) & df.cls.isin(["empty", "static"])].dropna(subset=FEATURES)
    rows = {}
    tr, te = temporal_split(df, 0.3)
    same = fit_predict(tr, te)
    held, miss_n, miss_d = {}, {}, {}
    for key, sess in HELD.items():
        tr2, te2 = temporal_split(df, 0.3, [sess])
        pred = fit_predict(tr2, te2)
        for name, p in pred.items():
            y = te2["cls"].to_numpy()
            held.setdefault(name, {})[key] = balanced_accuracy_score(y, p)
            g = (te2.session == sess).to_numpy()
            miss_n[name] = miss_n.get(name, 0) + int((p[g] == "empty").sum())
            miss_d[name] = miss_d.get(name, 0) + int(g.sum())
    order = ["다수 클래스(기준선)"] + [n for n in MODEL_KEYS if n in same]
    for name in order:
        h = held[name]
        rows[name] = {
            "같은 세션 정확도": accuracy_score(te["cls"], same[name]),
            "같은 세션 균형 정확도": balanced_accuracy_score(te["cls"], same[name]),
            "재실 A 제외": h["A"], "재실 B 제외": h["B"],
            "★ 처음 보는 세션 평균": np.mean(list(h.values())), "처음 보는 세션 최저": min(h.values()),
            "재실 놓침률": miss_n[name] / miss_d[name],
        }
    t = pd.DataFrame(rows).T
    t.attrs["n"] = {"same_test": f"빈 방 {int((te.cls == 'empty').sum())} · 재실 {int((te.cls == 'static').sum())}창",
                    "held_static": f"재실 A {int((df.session == HELD['A']).sum())} · B {int((df.session == HELD['B']).sum())}창",
                    "win": float(df['win'].iloc[0]), "rx": rx}
    return t


def to_markdown(t: pd.DataFrame) -> str:
    cols = list(t.columns)
    best = pick_best(t)
    lines = ["| 모델 | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for name, r in t.iterrows():
        cells = []
        for c in cols:
            v = f"{r[c]:.2f}" if c != "재실 놓침률" else f"{100 * r[c]:.0f}%"
            cells.append(f"**{v}**" if name == best else v)
        lines.append(f"| {'**' + name + '**' if name == best else name} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def pick_best(t: pd.DataFrame) -> str:
    """대표 모델: 처음 보는 세션 평균(소수 둘째 자리)이 가장 높고, 같으면 최저값이 높은 모델."""
    m = t.drop(index="다수 클래스(기준선)", errors="ignore")
    return max(m.index, key=lambda n: (round(m.loc[n].iloc[4], 2), m.loc[n].iloc[5]))


def to_png(t: pd.DataFrame, out: Path) -> None:
    plt.rcParams.update({"font.family": "Malgun Gothic", "axes.unicode_minus": False})
    n = t.attrs["n"]
    header = ["모델",
              "같은 세션 뒤 30%\n정확도", "같은 세션 뒤 30%\n균형 정확도",
              "처음 보는 세션\n재실 A 학습 제외", "처음 보는 세션\n재실 B 학습 제외",
              "★ 처음 보는 세션\n평균 (대표값)", "처음 보는 세션\n최저", "재실 놓침률\n(낮을수록 좋음)"]
    body = []
    for name, r in t.iterrows():
        body.append([name] + [f"{r.iloc[k]:.2f}" for k in range(6)] + [f"{100 * r.iloc[6]:.0f}%"])
    best = pick_best(t)
    fig = plt.figure(figsize=(13, 0.9 + 0.62 * (len(body) + 1)))
    fig.patch.set_facecolor(SURF)
    ax = fig.add_axes([0.01, 0.2, 0.98, 0.62])
    ax.axis("off")
    tb = ax.table(cellText=body, colLabels=header, cellLoc="center", bbox=[0, 0, 1, 1],
                  colWidths=[0.17, 0.11, 0.12, 0.13, 0.13, 0.13, 0.1, 0.11])
    tb.auto_set_font_size(False)
    tb.set_fontsize(10.5)
    for (i, j), cell in tb.get_celld().items():
        cell.set_edgecolor(GRID)
        cell.set_linewidth(0.8)
        base = body[i - 1][0] if i > 0 else ""
        if i == 0:
            cell.set_facecolor("#f0efec")
            cell.set_text_props(weight="bold", color=INK, fontsize=9)
        else:
            cell.set_facecolor(SURF)
            cell.set_text_props(color=INK2 if base.startswith("다수") else INK)
        if j == 5:
            cell.set_facecolor("#f6cdb9" if i == 0 else HL)
        if i > 0 and base == best:
            cell.set_text_props(weight="bold", color=INK)
        if j == 0 and i > 0:
            cell.set_text_props(ha="left")
            cell.PAD = 0.04
    fig.text(0.01, 0.93, f"빈 방 vs 정적 재실 판별 결과 — ESP32 CSI, RX {n['rx']}, {n['win']:g}초 창, 2026-11-21 데이터",
             ha="left", va="top", fontsize=12.5, fontweight="bold", color=INK)
    fig.text(0.01, 0.16,
             f"★ 대표값 = 재실 세션 2개({n['held_static']})를 하나씩 학습에서 통째로 빼고 평가한 균형 정확도의 평균. "
             f"굵은 행 = 대표 모델(평균이 같으면 최저값이 높은 쪽).\n"
             f"같은 세션 뒤 30% = 세션마다 시간순 앞 70% 학습 → 뒤 30% 테스트({n['same_test']}). 기준선 = 항상 '빈 방'이라고 답함. "
             f"재실 놓침률 = 처음 보는 재실 창을 빈 방으로 판단한 비율.\n"
             f"빈 방 세션은 하나뿐이라 빈 방 쪽은 같은 세션 뒤 30%로 평가됨 (처음 보는 빈 방은 미검증). 테스트는 모두 실측에서 떼어냄(합성 아님).",
             ha="left", va="top", fontsize=8.5, color=INK2, linespacing=1.6)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, facecolor=SURF)


def main() -> None:
    t = compute(ROOT / "features_win30s.csv")
    out_csv = ROOT / "results_table.csv"
    t.to_csv(out_csv, encoding="utf-8-sig", float_format="%.4f")
    png = ROOT.parent / "docs" / "figures" / "results_table.png"
    to_png(t, png)
    print(to_markdown(t))
    print(f"\n저장: {png}\n      {out_csv}")


if __name__ == "__main__":
    main()
