# -*- coding: utf-8 -*-
import os, re, time, json, hashlib, random, traceback, sys
# === START: 修正 NameError ===
from pathlib import Path # <--- 新增導入 Path
# === END: 修正 NameError ===
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urljoin

import requests, feedparser
from bs4 import BeautifulSoup
from openai import OpenAI
from opencc import OpenCC

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
cc = OpenCC("s2t")
HEADERS = {"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
MAX_AGE_DAYS = 3

KEYWORDS = {"上市":2,"IPO":2,"財報":1,"季報":1,"營收":1,"利潤":1,"成長":1,"破紀錄":2,
            "投資":2,"融資":1,"收購":2,"併購":2,"新遊戲":2,"發表":2,"全球":1,"海外拓展":1,
            "騰訊":1,"NetEase":1,"任天堂":1,"Sony":1,"Microsoft":1,"Google":1,"Apple":1,
            "破圈":1,"IP授權":2,"出海":1}
BLACKLIST = {"cosplay":-1,"八卦":-1,"爆料":-1,"玩家惡搞":-1,"公會事件":-1,"bug":-2,"外掛":-2,
             "攻略":-2,"彩蛋":-2,"小型比賽":-1}

SOURCES = [
    {"name":"遊戲葡萄", "rss":"http://youxiputao.com/feed"},
    {"name":"GameLook", "rss":"http://www.gamelook.com.cn/?feed=rss2"},
    {"name":"手游那點事","rss":"http://www2.nadianshi.com/feed"},
    {"name":"GameRes遊資網","rss":None,"url":"https://www.gameres.com/"},
    {"name":"巴哈姆特 GNN","rss":"https://gnn.gamer.com.tw/rss.xml"}
]

# === START: Debug Print Helper ===
def debug_print(msg):
    """自訂 print 函式，確保立即輸出"""
    print(msg, flush=True)
# === END: Debug Print Helper ===

def norm(x): return re.sub(r"\s+"," ",str(x or "")).strip()
def make_id(src,t,u): return hashlib.md5(f"{src}|{t}|{u}".encode("utf-8")).hexdigest()

def fetch_article(url):
    debug_print(f"[DEBUG] Fetching article content for: {url}") # <--- 新增日誌
    try:
        if "gameres.com" in url: time.sleep(random.uniform(1.5,3.0))
        r=requests.get(url,headers=HEADERS,timeout=20); r.raise_for_status()
        soup=BeautifulSoup(r.text,"lxml")

        title = cc.convert(soup.title.get_text(" ",strip=True) if soup.title else url)
        text = ""
        pub_date = None
        date_source = "None" # <--- 新增日誌

        def parse_date(date_str):
            if not date_str: return None, "No Date String"
            match = re.search(r'(\d{4})[-年](\d{1,2})[-月](\d{1,2})', date_str)
            if match:
                try:
                    y, m, d = [int(g) for g in match.groups()]
                    dt = datetime(y, m, d).date()
                    return dt, "Parsed from String"
                except: return None, "Parse Error"
            return None, "No Match"

        wechat_date_node = soup.select_one("#publish_time")
        if wechat_date_node:
            pub_date, date_source = parse_date(wechat_date_node.get_text())
            h1 = soup.select_one("h1#activity-name")
            body = soup.select_one("div.rich_media_content")
            if h1: title = cc.convert(h1.get_text(" ",strip=True))
            if body: text = norm(cc.convert(body.get_text(" ",strip=True)))
            debug_print(f"[DEBUG] fetch_article (WeChat): Date={pub_date} (Source: {date_source})") # <--- 新增日誌
            return title, text, pub_date

        gameres_h1 = soup.select_one("h1.article-title")
        if gameres_h1:
            title = cc.convert(gameres_h1.get_text(" ",strip=True))
            body = soup.select_one("div#maincontent")
            if body: text = norm(cc.convert(body.get_text(" ",strip=True)))
            meta_div = gameres_h1.find_next_sibling("div")
            if meta_div:
                pub_date, date_source = parse_date(meta_div.get_text())
            debug_print(f"[DEBUG] fetch_article (GameRes): Date={pub_date} (Source: {date_source})") # <--- 新增日誌
            return title, text, pub_date

        gnn_date_node = soup.select_one("span.GN-lbox3C")
        if gnn_date_node:
            pub_date, date_source = parse_date(gnn_date_node.get_text())
            h1 = soup.select_one("h1")
            body = soup.select_one("div.BH-lbox.GN-lbox3") or soup.select_one("div.GN-lbox3B")
            if h1: title = cc.convert(h1.get_text(" ",strip=True))
            if body: text = norm(cc.convert(body.get_text(" ",strip=True)))
            debug_print(f"[DEBUG] fetch_article (GNN): Date={pub_date} (Source: {date_source})") # <--- 新增日誌
            return title, text, pub_date

        for sel in ["article","div.article","div.content","div.post","#content","body"]:
            c = soup.select_one(sel)
            if c:
                temp_text = c.get_text(" ",strip=True)
                if len(temp_text)>200:
                    text = norm(cc.convert(temp_text))
                    break
        if not text: text = norm(cc.convert(soup.get_text(" ",strip=True)))
        debug_print(f"[DEBUG] fetch_article (Fallback): Date=None") # <--- 新增日誌
        return title, text, None

    except Exception as e:
        debug_print(f"[WARN] OpenAI 抓全文失敗 {url} {e}") # <--- 修改日誌
        return "", "", None

def keyword_score(txt):
    s=0
    for k,w in KEYWORDS.items():
        if k in txt: s+=w
    for b,w in BLACKLIST.items():
        if b in txt: s+=w
    return s

def recency_boost(pub_ts_iso):
    try: dt=datetime.fromisoformat(pub_ts_iso.replace("Z","+00:00"))
    except: return 0.0
    today=datetime.now(timezone.utc).date()
    d=(today - dt.date()).days
    if d<=0: return 0.5 # 今天
    if d==1: return 0.2 # 昨天
    return 0.0

def extract_json(txt):
    raw=(txt or "").strip()
    if raw.startswith("```json"): raw = raw[7:]
    if raw.endswith("```"): raw = raw[:-3]
    raw = raw.strip()
    m=re.search(r"\{.*\}", raw, flags=re.S)
    if m:
        try: return json.loads(m.group(0))
        except: pass
    try: return json.loads(raw)
    except:
        m = re.search(r"([0-9]+(\.[0-9]+)?)", raw) # 嘗試提取分數數字
        if m: return {"score": float(m.group(1))}
        return {}

def _write_raw(title, stage, raw):
    try:
        os.makedirs("data", exist_ok=True)
        payload = {"ts": datetime.now().isoformat(timespec="seconds"),"stage": stage,"title": title,"raw": raw}
        with open("data/news_raw.json","a",encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        debug_print(f"[WARN] 寫入 news_raw.json 失敗: {e}")

def ai_score(title,fulltext):
    debug_print(f"[DEBUG] AI Scoring (OpenAI): {title[:30]}...") # <--- 修改日誌
    text = fulltext[:1600]
    prompt=f"""你是一位專業的遊戲產業分析師，請判斷以下新聞對遊戲產業的重要性，給 1~5 分。
請積極區分，不要集中在 3。
若新聞影響很小或瑣碎，請給 1 或 2；
若新聞涉及重大產業或全球影響，請給 4 或 5；
一般情況才用 3。
分數務必使用一位小數（例如 2.3、4.7），數字必須是阿拉伯數字。
請務必只輸出 JSON。
回傳格式: {{"score": 數字}}

標題:{title}
內文:{text}"""
    raw = ""
    try:
        # 修正：使用 OpenAI 的 ChatCompletion
        r = client.chat.completions.create(
            model="gpt-4o-mini", # 或者您想用的其他模型
            messages=[{"role": "user", "content": prompt}],
            max_tokens=50, # 限制回答長度
            temperature=0, # 追求穩定
            response_format={"type": "json_object"} # 要求 JSON 輸出
        )
        msg = r.choices[0].message
        content = getattr(msg, "content", "")
        raw = (content or "").strip()
        debug_print(f"RAW(score): {raw[:200]}") # <--- 修改日誌
        _write_raw(title, "score", raw)
        obj = extract_json(raw)
        s = float(obj.get("score",3))
        s = max(1.0,min(5.0,s))
        debug_print(f"[DEBUG] AI Score result (OpenAI): {s}") # <--- 修改日誌
        return s
    except Exception as e:
        err = f"[ERROR ai_score OpenAI] {str(e)}\n{traceback.format_exc()}"; debug_print(err) # <--- 修改日誌
        _write_raw(title, "score", err)
        return 3.0

def ai_summary_and_impact(title,fulltext,retries=3):
    debug_print(f"[DEBUG] AI Summarizing (OpenAI): {title[:30]}...") # <--- 修改日誌
    text = fulltext
    prompt=f"""請務必只輸出 JSON，鍵名必須固定為英文，不可翻譯：
{{
 "summary":"一段話摘要，必須把原文中的關鍵信息跟核心意指描寫出來（繁體，<=300字）",
 "impact":"對市場/產業/玩家/業者的影響，必須深刻且是全方位分析過的結果（繁體，<=300字）"
}}
標題:{title}
內文:{text}"""
    for attempt in range(retries):
        try:
            r=client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content":prompt}],
                max_tokens=800,timeout=90,
                response_format={"type": "json_object"}
            )
            msg = r.choices[0].message
            content = getattr(msg, "content", "")
            raw = (content or "").strip()
            debug_print(f"RAW(summary): {raw[:200]}") # <--- 修改日誌
            _write_raw(title, "summary", raw)
            obj=extract_json(raw)
            s=cc.convert(obj.get("summary",""))
            i=cc.convert(obj.get("impact") or obj.get("影響") or obj.get("effect") or "")
            if s and i:
                 debug_print(f"[DEBUG] AI Summary result (OpenAI): OK") # <--- 修改日誌
                 return s,i
        except Exception as e:
            err = f"[ERROR ai_summary OpenAI (Attempt {attempt+1}/{retries})] {str(e)}\n{traceback.format_exc()}"; debug_print(err) # <--- 修改日誌
            _write_raw(title, "summary", err)
            time.sleep(1)
    debug_print(f"[DEBUG] AI Summary failed after {retries} retries (OpenAI).") # <--- 修改日誌
    return cc.convert(fulltext[:120] or "（無摘要）"), "（AI 未生成影響）"

def fetch_rss(src):
    out=[]
    today = datetime.now(timezone.utc).date()
    try:
        debug_print(f"[INFO] 正在抓取 RSS: {src['name']} ({src['rss']})") # <--- 修改日誌
        f=feedparser.parse(src["rss"])
        debug_print(f"[DEBUG] Found {len(f.entries)} entries in RSS feed for {src['name']}") # <--- 新增日誌
        for i, e in enumerate(f.entries):
            t_orig=norm(e.get("title")); u=e.get("link")
            debug_print(f"[DEBUG] Processing RSS entry {i+1}: {t_orig[:50]}...") # <--- 新增日誌
            if not u:
                debug_print("[DEBUG] Skipping entry: No link found.") # <--- 新增日誌
                continue

            article_date_from_feed = None
            published_parsed = e.get("published_parsed")
            if published_parsed:
                try: article_date_from_feed = datetime(*published_parsed[:6]).date()
                except: debug_print("[DEBUG] Failed to parse RSS published_parsed date.") # <--- 新增日誌
            debug_print(f"[DEBUG] Date from RSS feed: {article_date_from_feed}") # <--- 新增日誌

            if article_date_from_feed:
                 days_diff_rss = (today - article_date_from_feed).days
                 debug_print(f"[DEBUG] Days difference (RSS date vs Today): {days_diff_rss}") # <--- 新增日誌
                 if days_diff_rss > MAX_AGE_DAYS:
                    debug_print(f"[INFO] 過濾RSS舊聞 (>{MAX_AGE_DAYS} days old): {t_orig[:30]}...")
                    continue
            
            t, full, page_date = fetch_article(u)
            if not full:
                debug_print(f"[WARN] 無法抓取全文，跳過: {u}")
                continue
            debug_print(f"[DEBUG] Date parsed from article page: {page_date}") # <--- 新增日誌

            effective_date = page_date or article_date_from_feed
            debug_print(f"[DEBUG] Effective date for filtering: {effective_date}") # <--- 新增日誌
            if effective_date:
                days_diff_eff = (today - effective_date).days
                debug_print(f"[DEBUG] Days difference (Effective date vs Today): {days_diff_eff}") # <--- 新增日誌
                if days_diff_eff > MAX_AGE_DAYS:
                    debug_print(f"[INFO] 過濾頁面/RSS舊聞 (>{MAX_AGE_DAYS} days old): {t[:30]}...")
                    continue
            else:
                debug_print("[DEBUG] No effective date found for filtering, keeping article.") # <--- 新增日誌

            pub=datetime.now(timezone.utc).isoformat()
            if effective_date:
                pub = datetime(effective_date.year, effective_date.month, effective_date.day, tzinfo=timezone.utc).isoformat()
            else:
                 if published_parsed:
                    try: pub=datetime(*published_parsed[:6],tzinfo=timezone.utc).isoformat()
                    except: pass
            debug_print(f"[DEBUG] Final pub_ts for JSON: {pub}") # <--- 新增日誌
            
            out.append({"id":make_id(src["name"],t,u),"title":t,"url":u,"source":src["name"],
                        "summary":full[:300],"fulltext":full,"pub_ts":pub})
    except Exception as e:
        debug_print(f"[WARN] OpenAI RSS失敗 {src['name']} {e}") # <--- 修改日誌
    return out

def fetch_html(src,max_links=10): # <-- 修正: max_links 之前是 20
    out=[]; base=src["url"]; today = datetime.now(timezone.utc).date()
    try:
        debug_print(f"[INFO] 正在抓取 HTML: {src['name']} ({src['url']})") # <--- 修改日誌
        r=requests.get(base,headers=HEADERS,timeout=20); r.raise_for_status()
        s=BeautifulSoup(r.text,"lxml")
        
        latest_news_container = s.select_one('div.layui-tab-item.layui-show')
        if not latest_news_container:
            debug_print("[WARN] 找不到 GameRes '最新' 區塊，爬取主頁所有連結。") # <--- 修改日誌
            latest_news_container = s 
        
        domain=urlparse(base).netloc
        links=[]
        processed_urls = set()

        for a in latest_news_container.find_all("a",href=True):
            u=urljoin(base,a["href"]).split("#")[0]
            if u in processed_urls: continue 
            processed_urls.add(u)

            if urlparse(u).netloc!=domain: continue
            if "gameres.com" in domain and not re.search(r"/\d+\.html$", urlparse(u).path or ""):
                continue

            links.append(u)
            if len(links)>=max_links: break

        debug_print(f"[INFO] 從 {src['name']} 找到 {len(links)} 個潛在文章連結。") # <--- 修改日誌
        for i, u in enumerate(links):
            debug_print(f"[DEBUG] Processing HTML link {i+1}/{len(links)}: {u}") # <--- 新增日誌
            title, full, page_date = fetch_article(u)
            if not full:
                debug_print(f"[WARN] 無法抓取全文，跳過: {u}")
                continue
            debug_print(f"[DEBUG] Date parsed from article page: {page_date}") # <--- 新增日誌

            if page_date:
                days_diff_page = (today - page_date).days
                debug_print(f"[DEBUG] Days difference (Page date vs Today): {days_diff_page}") # <--- 新增日誌
                if days_diff_page > MAX_AGE_DAYS:
                    debug_print(f"[INFO] 過濾頁面舊聞 (>{MAX_AGE_DAYS} days old): {title[:30]}...")
                    continue
            else:
                 debug_print("[DEBUG] No page date found for filtering, keeping article.") # <--- 新增日誌
            
            pub=datetime.now(timezone.utc).isoformat()
            if page_date:
                pub = datetime(page_date.year, page_date.month, page_date.day, tzinfo=timezone.utc).isoformat()
            debug_print(f"[DEBUG] Final pub_ts for JSON: {pub}") # <--- 新增日誌
            
            out.append({"id":make_id(src["name"],title,u),"title":title,"url":u,"source":src["name"],
                        "summary":full[:300],"fulltext":full,"pub_ts":pub})
    except Exception as e:
        debug_print(f"[WARN] OpenAI HTML失敗 {src['name']} {e}") # <--- 修改日誌
    return out

def main():
    debug_print("=== fetch_news_rss.py Start ===") # <--- 修改日誌

    # === START: 檔名修改 ===
    today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    output_filename = f"news_ai_{today_str}.json"
    output_path = Path("data") / output_filename # <-- Path 物件現在可用了
    # === END: 檔名修改 ===

    items=[]
    for src in SOURCES:
        try:
            items_from_source = fetch_rss(src) if src.get("rss") else fetch_html(src)
            items.extend(items_from_source)
            debug_print(f"[INFO] 從 {src['name']} 抓取到 {len(items_from_source)} 篇文章。")
        except Exception as e:
             debug_print(f"[FATAL ERROR] 處理來源 {src['name']} 時發生嚴重錯誤: {e}")
        time.sleep(0.5)

    debug_print(f"[INFO] 所有來源共抓取到 {len(items)} 篇文章（初步）。")

    seen_ids=set(); valid_items=[]
    today=datetime.now(timezone.utc).date()
    debug_print(f"[DEBUG] Starting filtering/deduplication. Today (UTC): {today}") # <--- 新增日誌
    for i, it in enumerate(items):
        item_id = it.get("id")
        title = it.get("title", "N/A")[:30]

        if item_id in seen_ids: continue
        if not it.get("title") or not it.get("fulltext"): continue
            
        try:
            pub_ts = it.get("pub_ts", "")
            item_date = datetime.fromisoformat(pub_ts.replace("Z","+00:00")).date()
            days_diff_final = (today - item_date).days
            if days_diff_final > MAX_AGE_DAYS: continue
        except Exception as e:
            debug_print(f"[WARN] Skipping item due to date parse error: {pub_ts} - {e}") # <--- 修改日誌
            continue 

        valid_items.append(it)
        seen_ids.add(item_id)

    debug_print(f"[INFO] 過濾與去重後剩下 {len(valid_items)} 篇文章。")
    if not valid_items:
        debug_print("[WARN] 沒有任何有效文章符合條件，AI 分析將不會執行。") # <--- 新增日誌

    final_items = []
    debug_print(f"[INFO] 開始 AI 分析與評分 (共 {len(valid_items)} 篇)...") # <--- 新增日誌
    for i, it in enumerate(valid_items):
        debug_print(f"[DEBUG] Processing item {i+1}/{len(valid_items)} for AI score...") # <--- 新增日誌
        ks=min(keyword_score(it["title"]+" "+it["fulltext"]), 10) # <-- 修正：之前是 min(0,10)
        s=ai_score(it["title"],it["fulltext"])
        rb=recency_boost(it["pub_ts"])
        it["keyword_score"]=ks; it["ai_score"]=s
        it["final_score"]=(s*3 + ks) * (1+rb)
        final_items.append(it)
        time.sleep(0.5)

    final_items.sort(key=lambda x:(x.get("final_score", 0), x.get("ai_score", 0), x.get("pub_ts", "")), reverse=True)
    debug_print(f"[INFO] 完成 AI 評分與排序，共 {len(final_items)} 篇有效文章。")

    items_to_summarize = final_items[:20]
    debug_print(f"[INFO] 開始為 Top {len(items_to_summarize)} 文章生成摘要與影響力...") # <--- 新增日誌
    for i, it in enumerate(items_to_summarize):
        debug_print(f"[DEBUG] Processing item {i+1}/{len(items_to_summarize)} for AI summary...") # <--- 新增日誌
        s,i=ai_summary_and_impact(it["title"],it["fulltext"])
        it["ai_summary"]=s; it["ai_impact"]=i
        # 可以選擇移除全文以節省空間
        # it["fulltext"] = ""
        time.sleep(1)

    top10_ids=[it["id"] for it in final_items[:10]]
    output_data = {"all":final_items,"top10":top10_ids}
    
    os.makedirs("data",exist_ok=True)
    try:
        # === START: 檔名修改 ===
        with open(output_path,"w",encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False,indent=2)
        debug_print(f"[OK] 成功將 {len(final_items)} 篇文章寫入 {output_path}") # <--- 使用新檔名
        # === END: 檔名修改 ===
    except Exception as e:
        debug_print(f"[FATAL ERROR] 無法寫入最終 JSON 檔案 {output_path}: {e}") # <--- 修改日誌

    debug_print("=== fetch_news_rss.py End ===") # <--- 修改日誌

if __name__=="__main__": main()
