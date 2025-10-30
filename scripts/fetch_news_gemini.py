# -*- coding: utf-8 -*-
import os, re, time, json, hashlib, random
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urljoin

import requests, feedparser
from bs4 import BeautifulSoup
import google.generativeai as genai
from opencc import OpenCC

# ========== 初始化 ==========
genai.configure(api_key=os.getenv("GEMINI") or os.getenv("GEMINI_API_KEY"))
GEN_MODEL=os.getenv("GEMINI_MODEL","gemini-2.5-flash")
cc=OpenCC("s2t")
HEADERS={"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
MAX_AGE_DAYS = 3

SOURCES=[
    {"name":"遊戲葡萄","rss":"http://youxiputao.com/feed"},
    {"name":"GameLook","rss":"http://www.gamelook.com.cn/?feed=rss2"},
    {"name":"手游那點事","rss":"http://www2.nadianshi.com/feed"},
    {"name":"GameRes遊資網","rss":None,"url":"https://www.gameres.com/"},
    {"name":"巴哈姆特 GNN","rss":"https://gnn.gamer.com.tw/rss.xml"}
]

def norm(x): return re.sub(r"\s+"," ",str(x or "")).strip()
def make_id(src,t,u): return hashlib.md5(f"{src}|{t}|{u}".encode("utf-8")).hexdigest()

# <<< 修正點：整合多種版型解析邏輯 >>>
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
            match = re.search(r'(\d{4})[-年](\d{1,2})[-月](\d{1,2})', date_str)
            if match:
                try:
                    y, m, d = [int(g) for g in match.groups()]
                    return datetime(y, m, d).date()
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
        print(f"[WARN] GEM 抓全文失敗 {url} {e}")
        return "", "", None

def sanitize_text(s,limit=120):
    s=cc.convert(str(s or "")).strip()
    s=re.sub(r"\s+"," ",s)
    return s[:limit] if s else s

def extract_json(txt):
    raw=(txt or "").strip()
    m=re.search(r"\{.*\}", raw, flags=re.S)
    if m:
        try: return json.loads(m.group(0))
        except: pass
    try: return json.loads(raw)
    except: return {}

def ai_score(title,fulltext):
    text=fulltext[:1600]
    prompt=f"""你是一個專業的遊戲產業大師，請判斷以下新聞對遊戲產業的重要性，給 1~5 分。
請積極區分，不要集中在 3。
若新聞影響很小或瑣碎，請給 1 或 2；
若新聞涉及重大產業或全球影響，請給 4 或 5；
一般情況才用 3。
分數務必使用一位小數，分數可用 0.1 的精度，例如 2.3、4.7
請務必只輸出 JSON，格式：{{"score": 數字}}。
標題:{title}
內文:{text}"""
    try:
        m=genai.GenerativeModel(GEN_MODEL)
        r=m.generate_content(prompt,request_options={"timeout":90})
        obj=extract_json(r.text)
        s=float(obj.get("score",3))
        s=max(1.0,min(5.0,s))
        return s
    except Exception as e:
        print(f"[WARN] GEM 打分失敗 {e}")
        return 3.0

def ai_summary_and_impact(title,fulltext,retries=2):
    text=fulltext
    prompt=f"""請務必只輸出 JSON，鍵名必須固定為英文，不可翻譯：
{{
 "summary":"一句話摘要（繁體，<=120字）",
 "impact":"對市場/產業/玩家/業者的影響（繁體，<=120字）"
}}
標題:{title}
內文:{text}"""
    for _ in range(retries):
        try:
            m=genai.GenerativeModel(GEN_MODEL)
            r=m.generate_content(prompt,request_options={"timeout":120})
            obj=extract_json(r.text)
            s=sanitize_text(obj.get("summary",""))
            i=sanitize_text(obj.get("impact") or obj.get("影響") or obj.get("effect") or "")
            if not s: s="（Gemini 未生成摘要）"
            if not i: i="（Gemini 未生成影響）"
            return s,i
        except Exception as e:
            print(f"[WARN] GEM 摘要失敗 {e}")
            time.sleep(1)
    return sanitize_text(fulltext[:120] or "（無摘要）"), "（Gemini 未生成影響）"

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
        print(f"[WARN] GEM RSS失敗 {src['name']} {e}")
    return out

def fetch_html(src,max_links=20):
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
            if u not in links:
                links.append(u)
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
        print(f"[WARN] GEM HTML失敗 {src['name']} {e}")
    return out

def main():
    print("=== fetch_news_gemini.py Start ===")
    items=[]
    for src in SOURCES:
        items+=(fetch_rss(src) if src.get("rss") else fetch_html(src))
        time.sleep(0.5)

    today=datetime.now(timezone.utc).date()
    def to_date(iso): return datetime.fromisoformat(iso.replace("Z","+00:00")).date()
    today_items=[it for it in items if to_date(it["pub_ts"])==today]
    yesterday_items=[it for it in items if to_date(it["pub_ts"])==today-timedelta(days=1)]

    if len(today_items)>=50: uniq=today_items[:100]
    else: uniq=(today_items+yesterday_items)[:50]
    if len(uniq)<50: uniq=uniq+uniq[:(50-len(uniq))]

    seen=set(); final=[]
    for it in uniq:
        if it["id"] not in seen: final.append(it); seen.add(it["id"])

    for it in final:
        ks=min(0, 10)
        s=ai_score(it["title"],it["fulltext"])
        rb=0.0
        it["ai_score"]=s
        it["final_score"]=(s*3 + ks) * (1+rb)

    final.sort(key=lambda x:(x["final_score"], x["ai_score"], x["pub_ts"]), reverse=True)

    for it in final[:20]:
        s,i=ai_summary_and_impact(it["title"],it["fulltext"])
        it["ai_summary"]=s; it["ai_impact"]=i

    top10=[it["id"] for it in final[:10]]
    os.makedirs("data",exist_ok=True)
    with open("data/news_gemini.json","w",encoding="utf-8") as f:
        json.dump({"all":final,"top10":top10},f,ensure_ascii=False,indent=2)
    print("[OK] wrote data/news_gemini.json")

if __name__=="__main__": main()
