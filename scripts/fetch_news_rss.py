# -*- coding: utf-8 -*-
import os, re, time, json, hashlib, random, traceback, sys
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

KEYWORDS = {"上市":3,"IPO":3,"財報":3,"季報":3,"營收":2,"利潤":2,"成長":2,"破紀錄":2,
            "投資":2,"融資":2,"收購":2,"併購":2,"新遊戲":2,"發表":2,"全球":1,"海外拓展":1,
            "騰訊":2,"NetEase":2,"任天堂":2,"Sony":1,"Microsoft":1,"Google":2,"Apple":2,
            "破圈":2,"IP授權":2,"出海":1}
BLACKLIST = {"cosplay":-2,"八卦":-2,"爆料":-2,"玩家惡搞":-2,"公會事件":-1,"bug":-1,"外掛":-1,
             "攻略":-2,"彩蛋":-2,"小型比賽":-1}

SOURCES = [
    {"name":"遊戲葡萄", "rss":"http://youxiputao.com/feed"},
    {"name":"GameLook", "rss":"http://www.gamelook.com.cn/?feed=rss2"},
    {"name":"手游那點事","rss":"http://www2.nadianshi.com/feed"},
    {"name":"GameRes遊資網","rss":None,"url":"https://www.gameres.com/"},
    {"name":"巴哈姆特 GNN","rss":"https://gnn.gamer.com.tw/rss.xml"}
]

def norm(x): return re.sub(r"\s+"," ",str(x or "")).strip()
def make_id(src,t,u): return hashlib.md5(f"{src}|{t}|{u}".encode("utf-8")).hexdigest()

def fetch_article(url):
    try:
        if "gameres.com" in url: time.sleep(random.uniform(1.5,3.0))
        r=requests.get(url,headers=HEADERS,timeout=20); r.raise_for_status()
        soup=BeautifulSoup(r.text,"lxml")

        title = cc.convert(soup.title.get_text(" ",strip=True) if soup.title else url)
        text = ""
        pub_date = None

        def parse_date(date_str):
            if not date_str: return None
            match = re.search(r'(\d{4}-\d{2}-\d{2})', date_str)
            if match:
                try: return datetime.strptime(match.group(1), '%Y-%m-%d').date()
                except: return None
            return None

        # --- 規則一：尋找微信文章版型 ---
        wechat_date_node = soup.select_one("#publish_time")
        if wechat_date_node:
            pub_date = parse_date(wechat_date_node.get_text())
            h1 = soup.select_one("h1#activity-name")
            body = soup.select_one("div.rich_media_content")
            if h1: title = cc.convert(h1.get_text(" ",strip=True))
            if body: text = norm(cc.convert(body.get_text(" ",strip=True)))
            return title, text, pub_date

        # --- 規則二：尋找 GameRes 原生版型 ---
        gameres_h1 = soup.select_one("h1.article-title")
        if gameres_h1:
            title = cc.convert(gameres_h1.get_text(" ",strip=True))
            body = soup.select_one("div#maincontent")
            if body: text = norm(cc.convert(body.get_text(" ",strip=True)))
            meta_div = gameres_h1.find_next_sibling("div")
            if meta_div:
                pub_date = parse_date(meta_div.get_text())
            return title, text, pub_date

        # --- 規則三：尋找 巴哈GNN 版型 ---
        gnn_date_node = soup.select_one("span.GN-lbox3C")
        if gnn_date_node:
            pub_date = parse_date(gnn_date_node.get_text())
            h1 = soup.select_one("h1")
            body = soup.select_one("div.BH-lbox.GN-lbox3") or soup.select_one("div.GN-lbox3B")
            if h1: title = cc.convert(h1.get_text(" ",strip=True))
            if body: text = norm(cc.convert(body.get_text(" ",strip=True)))
            return title, text, pub_date

        # --- 通用後備規則 ---
        for sel in ["article","div.article","div.content","div.post","#content","body"]:
            c = soup.select_one(sel)
            if c:
                temp_text = c.get_text(" ",strip=True)
                if len(temp_text)>200:
                    text = norm(cc.convert(temp_text))
                    break
        if not text: text = norm(cc.convert(soup.get_text(" ",strip=True)))
        return title, text, None

    except Exception as e:
        print(f"[WARN] 抓取文章失敗 {url} {e}")
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
    if d<=0: return 0.5
    if d==1: return 0.2
    return 0.0

def extract_json(txt):
    try:
        match = re.search(r"\{.*\}", txt, flags=re.S)
        if match: return json.loads(match.group(0))
        return json.loads(txt)
    except:
        m = re.search(r"([0-9]+(\.[0-9]+)?)", txt)
        if m: return {"score": float(m.group(1))}
        return {}

def _write_raw(title, stage, raw):
    try:
        os.makedirs("data", exist_ok=True)
        payload = {"ts": datetime.now().isoformat(timespec="seconds"),"stage": stage,"title": title,"raw": raw}
        with open("data/news_raw.json","a",encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[WARN] 寫入 news_raw.json 失敗: {e}")

def ai_score(title,fulltext):
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
        r = client.responses.create(
            model="gpt-5",
            input=prompt,
            max_output_tokens=500
        )
        raw = getattr(r, "output_text", "") or ""
        if not raw and getattr(r, "output", None):
            try: raw = r.output[0].content[0].text
            except: raw = ""
        print("RAW(score):", raw[:200]); sys.stdout.flush()
        _write_raw(title, "score", raw)
        obj = extract_json((raw or "").strip())
        s = float(obj.get("score",3))
        s = max(1.0,min(5.0,s))
        return s
    except Exception as e:
        err = f"[ERROR ai_score] {str(e)}\n{traceback.format_exc()}"; print(err); sys.stdout.flush()
        _write_raw(title, "score", err)
        return 3.0

def ai_summary_and_impact(title,fulltext,retries=3):
    text = fulltext
    prompt=f"""請務必只輸出 JSON，不要有任何多餘文字或省略號。
{{
 "summary":"一句話摘要（繁體，<=120字）",
 "impact":"對市場/產業/玩家/業者的影響（繁體，<=120字）"
}}
標題:{title}
內文:{text}"""
    for attempt in range(retries):
        try:
            r=client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content":prompt}],
                max_completion_tokens=400,timeout=90,
                response_format={"type": "json_object"}
            )
            msg = r.choices[0].message
            content = getattr(msg, "content", "")
            raw = (content or "").strip()
            print("RAW(summary):", raw[:200]); sys.stdout.flush()
            _write_raw(title, "summary", raw)
            obj=extract_json(raw)
            s=cc.convert(obj.get("summary",""))
            i=cc.convert(obj.get("impact","") or obj.get("影響","") or obj.get("effect",""))
            if s and i: return s,i
        except Exception as e:
            err = f"[ERROR ai_summary] {str(e)}\n{traceback.format_exc()}"; print(err); sys.stdout.flush()
            _write_raw(title, "summary", err)
            time.sleep(1)
    return cc.convert(fulltext[:120] or "（無摘要）"), "（AI 未生成影響）"

def fetch_rss(src):
    out=[]
    today = datetime.now(timezone.utc).date()
    try:
        f=feedparser.parse(src["rss"])
        for e in f.entries:
            t_orig=norm(e.get("title")); u=e.get("link")
            if not u: continue

            article_date_from_feed = None
            if e.get("published_parsed"):
                try: article_date_from_feed = datetime(*e.published_parsed[:6]).date()
                except: pass
            
            if article_date_from_feed and (today - article_date_from_feed).days > MAX_AGE_DAYS:
                print(f"[INFO] 過濾RSS舊聞 ({article_date_from_feed}): {t_orig[:30]}...")
                continue
            
            t, full, page_date = fetch_article(u)
            if page_date and (today - page_date).days > MAX_AGE_DAYS:
                print(f"[INFO] 過濾頁面舊聞 ({page_date}): {t[:30]}...")
                continue

            pub=datetime.now(timezone.utc).isoformat()
            effective_date = page_date or article_date_from_feed
            if effective_date:
                pub = datetime(effective_date.year, effective_date.month, effective_date.day, tzinfo=timezone.utc).isoformat()
            else:
                if e.get("published_parsed"):
                    try: pub=datetime(*e.published_parsed[:6],tzinfo=timezone.utc).isoformat()
                    except: pass
            
            out.append({"id":make_id(src["name"],t,u),"title":t,"url":u,"source":src["name"],
                        "summary":full[:300],"fulltext":full,"pub_ts":pub})
    except Exception as e:
        print(f"[WARN] RSS失敗 {src['name']} {e}")
    return out

def fetch_html(src,max_links=10):
    out=[]; base=src["url"]; today = datetime.now(timezone.utc).date()
    try:
        r=requests.get(base,headers=HEADERS,timeout=20); r.raise_for_status()
        s=BeautifulSoup(r.text,"lxml")
        
        latest_news_container = s.select_one('div.layui-tab-item.layui-show')
        if not latest_news_container:
            print("[WARN] 找不到 GameRes '最新' 區塊，爬取主頁所有連結。")
            latest_news_container = s 
        
        domain=urlparse(base).netloc
        links=[]
        for a in latest_news_container.find_all("a",href=True):
            u=urljoin(base,a["href"]).split("#")[0]
            if urlparse(u).netloc!=domain: continue
            if "gameres.com" in domain and not re.search(r"/\d+\.html$", urlparse(u).path or ""):
                continue
            if u not in links: links.append(u)
            if len(links)>=max_links: break

        for u in links:
            title, full, page_date = fetch_article(u)
            if not full: continue

            if page_date and (today - page_date).days > MAX_AGE_DAYS:
                print(f"[INFO] 過濾頁面舊聞 ({page_date}): {title[:30]}...")
                continue

            pub=datetime.now(timezone.utc).isoformat()
            if page_date:
                pub = datetime(page_date.year, page_date.month, page_date.day, tzinfo=timezone.utc).isoformat()
            
            out.append({"id":make_id(src["name"],title,u),"title":title,"url":u,"source":src["name"],
                        "summary":full[:300],"fulltext":full,"pub_ts":pub})
    except Exception as e:
        print(f"[WARN] HTML失敗 {src['name']} {e}")
    return out

def main():
    print("=== fetch_news_rss.py Start ===")
    items=[]
    for src in SOURCES:
        items+=(fetch_rss(src) if src.get("rss") else fetch_html(src))
        time.sleep(0.5)

    today=datetime.now(timezone.utc).date()
    today_items=[it for it in items if datetime.fromisoformat(it["pub_ts"].replace("Z","+00:00")).date()==today]
    yesterday_items=[it for it in items if datetime.fromisoformat(it["pub_ts"].replace("Z","+00:00")).date()==today-timedelta(days=1)]

    if len(today_items)>=50:
        uniq=today_items[:100]
    else:
        uniq=today_items+yesterday_items
        uniq=uniq[:50]

    seen=set(); final=[]
    for it in uniq:
        if it["id"] not in seen: final.append(it); seen.add(it["id"])

    for it in final:
        ks=min(keyword_score(it["title"]+" "+it["fulltext"]), 10)
        s=ai_score(it["title"],it["fulltext"])
        rb=recency_boost(it["pub_ts"])
        it["keyword_score"]=ks; it["ai_score"]=s
        it["final_score"]=(s*3 + ks) * (1+rb)

    final.sort(key=lambda x:(x["final_score"], x["ai_score"], x["pub_ts"]), reverse=True)

    for it in final[:20]:
        s,i=ai_summary_and_impact(it["title"],it["fulltext"])
        it["ai_summary"]=s; it["ai_impact"]=i

    top10=[it["id"] for it in final[:10]]
    os.makedirs("data",exist_ok=True)
    with open("data/news_ai.json","w",encoding="utf-8") as f:
        json.dump({"all":final,"top10":top10},f,ensure_ascii=False,indent=2)
    print("[OK] wrote data/news_ai.json")

if __name__=="__main__": main()
