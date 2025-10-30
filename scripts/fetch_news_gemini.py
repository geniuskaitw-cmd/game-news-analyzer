# -*- coding: utf-8 -*-
"""
analyze_ratings.py
1. 從 Firestore 抓取 news_ratings 集合
2. 輸出成 CSV（舊功能，完整保留）
3. 額外輸出 raw.json + analyzed.json
需要 FIREBASE_SERVICE_ACCOUNT 環境變數（JSON 字串）
"""

import os, json, argparse, csv
import firebase_admin
from firebase_admin import credentials, firestore
from statistics import mean

def init_firestore():
    sa_json = os.getenv("FIREBASE_SERVICE_ACCOUNT")
    if not sa_json:
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT 環境變數未設定")
    creds = credentials.Certificate(json.loads(sa_json))
    if not firebase_admin._apps:
        firebase_admin.initialize_app(creds)
    return firestore.client()

def fetch_ratings():
    db = init_firestore()
    docs = db.collection("news_ratings").stream()
    rows = []
    for d in docs:
        data = d.to_dict()
        rows.append({
            "news_id": data.get("news_id",""),
            "model_version": data.get("model_version",""),
            "score": data.get("score",""),
            "user_id": data.get("user_id",""),
            "date_str": data.get("date_str",""),
            "created_at": str(data.get("created_at", ""))
        })
    return rows

def export_csv(rows, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["news_id","model_version","score","user_id","date_str","created_at"])
        w.writeheader()
        for r in rows: w.writerow(r)
    print(f"[OK] 輸出 CSV {len(rows)} 筆 → {out_path}")

def export_json(rows, raw_path, analyzed_path):
    os.makedirs(os.path.dirname(raw_path), exist_ok=True)

    # raw: 直接輸出所有評分資料
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    print(f"[OK] 輸出 RAW JSON {len(rows)} 筆 → {raw_path}")

    # 分析：統計 AI vs Human 分差
    analysis = {}
    scores_by_version = {}
    for r in rows:
        mv = r.get("model_version") or "unknown"
        try:
            score = float(r.get("score"))
        except:
            continue
        scores_by_version.setdefault(mv, []).append(score)

    stats = {}
    for mv, vals in scores_by_version.items():
        stats[mv] = {
            "count": len(vals),
            "avg_score": mean(vals) if vals else None
        }

    # 如果有 ai & ft，可以算平均差
    if "ai" in stats and "ft" in stats:
        stats["ai_vs_ft_diff"] = stats["ai"]["avg_score"] - stats["ft"]["avg_score"]

    analysis["summary"] = stats
    analysis["total_records"] = len(rows)

    with open(analyzed_path, "w", encoding="utf-8") as f:
        json.dump({"rows": rows, "analysis": analysis}, f, ensure_ascii=False, indent=2)
    print(f"[OK] 輸出 ANALYZED JSON → {analyzed_path}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="輸出 CSV 路徑")
    ap.add_argument("--raw_json", required=False, help="輸出 raw JSON 路徑")
    ap.add_argument("--analyzed_json", required=False, help="輸出分析 JSON 路徑")
    args = ap.parse_args()

    rows = fetch_ratings()
    export_csv(rows, args.out)
    if args.raw_json and args.analyzed_json:
        export_json(rows, args.raw_json, args.analyzed_json)

if __name__ == "__main__":
    main()
