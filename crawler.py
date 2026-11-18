"""
屏科大「課程資料查詢」爬蟲
用法：
    python crawler.py              # 爬資管系（115 學年第 1 學期）
    python crawler.py --all        # 爬全部系所
    python crawler.py --list       # 列出所有系所代碼
    python crawler.py --dept 58    # 爬其他系所，例如 58 = 企管系
    python crawler.py --year 114 --term 2
輸出：
    data/courses_raw.csv  網頁表格原樣（一列 = 一個上課時段）
    data/courses.csv      整理後（一列 = 一門開課），給 rag.py 使用
"""
import argparse
import os
import re
import time

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

ENTRY = "https://course.npust.edu.tw/Cnc/Reports/OpenDept"   # 用瀏覽器進得去的入口
URL = "https://course.npust.edu.tw/Cnc/Reports/QueryCourseforStud"
P = "ctl00$MainContent$"          # ASP.NET 表單欄位的共同前綴
DELAY = 1.5                        # 每次請求之間停幾秒，避免造成學校伺服器負擔

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9",
})

# 學校系統要先從瀏覽器進入查詢頁才有權限。把瀏覽器的 Cookie 放進 .env 的 NPUST_COOKIE，
# 程式就能沿用同一個身分（Cookie 只放 .env，不要上傳 GitHub）
load_dotenv()
if os.getenv("NPUST_COOKIE"):
    session.headers["Cookie"] = os.getenv("NPUST_COOKIE").strip()


# ===== 工具函式 =====
def get_soup(resp):
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return BeautifulSoup(resp.text, "html.parser")


def form_fields(soup):
    """把網頁表單目前的所有欄位值收集起來（模擬瀏覽器送出表單時會帶的資料）"""
    form = soup.find("form", id="Form1")
    data = {}
    for inp in form.find_all("input"):
        name, typ = inp.get("name"), inp.get("type", "text")
        if not name or inp.has_attr("disabled") or typ in ("submit", "image", "button"):
            continue
        if typ in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                data[name] = inp.get("value", "on")
        else:
            data[name] = inp.get("value", "")
    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name or sel.has_attr("disabled"):
            continue
        opt = sel.find("option", selected=True) or sel.find("option")
        data[name] = opt.get("value", "") if opt else ""
    return data


def list_depts(soup):
    """回傳 [(系所代碼, 系所名稱), ...]，已去除重複"""
    sel = soup.find("select", {"name": P + "DropDownListDept"})
    seen, depts = set(), []
    for opt in sel.find_all("option"):
        code = opt.get("value", "")
        if code and code not in seen:
            seen.add(code)
            depts.append((code, opt.get_text(strip=True)))
    return depts


def parse_table(soup):
    """把查詢結果表格轉成 list of dict；沒有結果時回傳空清單"""
    table = soup.find("table", id="MainContent_GridView1")
    if table is None:
        return []
    heads = [th.get_text(strip=True) for th in table.find_all("th")]
    rows = []
    for tr in table.find_all("tr", recursive=False) or table.find("tbody").find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if len(tds) != len(heads):      # 標題列或分頁列，跳過
            continue
        row = {}
        for h, td in zip(heads, tds):
            cb = td.find("input", type="checkbox")
            row[h] = ("Y" if cb.has_attr("checked") else "N") if cb else td.get_text(strip=True)
        rows.append(row)
    return rows


def has_page(soup, n):
    """結果表格下方的分頁列有沒有第 n 頁的連結"""
    return f"'Page${n}'" in str(soup.find("table", id="MainContent_GridView1") or "")


# ===== 主要流程 =====
def open_page():
    # 先像瀏覽器一樣從入口網址進去，讓學校系統建立訪客身分，再打開「課程資料查詢」
    if not os.getenv("NPUST_COOKIE"):
        entry = session.get(ENTRY, timeout=30)
        time.sleep(DELAY)
    resp = session.get(URL, timeout=30)
    soup = get_soup(resp)
    if soup.find("select", {"name": P + "DropDownListDept"}) is None:
        # 進不去：把兩個頁面存下來，方便除錯
        if not os.getenv("NPUST_COOKIE"):
            open("debug_entry.html", "w", encoding="utf-8").write(entry.text)
        open("debug_query.html", "w", encoding="utf-8").write(resp.text)
        print("已存下 debug_entry.html、debug_query.html，可傳給 AI 協助判斷。")
        raise SystemExit("被導到登入頁了：請照手冊說明，把瀏覽器的 Cookie 貼到 .env 的 NPUST_COOKIE（Cookie 過期就重新複製一次）。")
    return soup


def crawl_dept(code, name, year, term):
    soup = open_page()
    time.sleep(DELAY)

    data = form_fields(soup)
    data.update({
        P + "DropDownListAca": str(year),
        P + "DropDownListTerm": str(term),
        P + "ChkDept": "on",
        P + "DropDownListDept": code,
        P + "Btnok": "確定",
    })
    soup = get_soup(session.post(URL, data=data, timeout=60))
    rows = parse_table(soup)

    page = 1
    while has_page(soup, page + 1):          # 還有下一頁就繼續翻
        page += 1
        time.sleep(DELAY)
        data = form_fields(soup)
        data.update({"__EVENTTARGET": P + "GridView1", "__EVENTARGUMENT": f"Page${page}"})
        soup = get_soup(session.post(URL, data=data, timeout=60))
        rows += parse_table(soup)

    print(f"  {name}（{code}）：{page} 頁、{len(rows)} 列")
    return rows


# ===== 整理成 rag.py 用的格式 =====
WEEK = {"1": "一", "2": "二", "3": "三", "4": "四", "5": "五", "6": "六", "7": "日"}
CN_NUM = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5"}


def tidy(raw, term):
    df = pd.DataFrame(raw)
    df = df[df["開停"] != "停"]                     # 去掉停開的課
    out = []
    for seq, g in df.groupby("序號", sort=False):
        r = g.iloc[0]
        slots = [f"週{WEEK.get(w, w)}第{p}節" for w, p in zip(g["星期"], g["節次"])
                 if w in WEEK and p not in ("", "X")]
        m = re.search(r"([一二三四五])[A-Z]?$", r["班級"])
        out.append({
            "course_id": seq,                       # 序號：每個開課班級唯一
            "code": r["課程代碼"],
            "name": r["課程名稱"],
            "dept": r["開課系所"],
            "class_name": r["班級"],
            "year": CN_NUM.get(m.group(1), "") if m else "",
            "semester": "上" if str(term) == "1" else "下",
            "type": "必修" if r["修別"] == "必" else "選修",
            "credits": r["學分"].replace(".0", ""),
            "teacher": "、".join(dict.fromkeys(g["授課教師"])),
            "schedule": "、".join(dict.fromkeys(slots)) or "未排定",
            "room": "、".join(dict.fromkeys(x for x in g["教室"] if x)),
            "program": r["學程"],
            "english": "是" if r["英文"] == "1" else "否",
            "prereq": "",
            "track": r["學程"],
            "description": "",
        })
    return pd.DataFrame(out).drop_duplicates("course_id")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", default="115")
    ap.add_argument("--term", default="1")
    ap.add_argument("--dept", default="56", help="系所代碼，預設 56 = 資管系")
    ap.add_argument("--all", action="store_true", help="爬全部系所")
    ap.add_argument("--list", action="store_true", help="列出系所代碼後結束")
    args = ap.parse_args()

    depts = list_depts(open_page())
    if args.list:
        for code, name in depts:
            print(code, name)
        raise SystemExit
    if not args.all:
        depts = [d for d in depts if d[0] == args.dept]

    print(f"開始爬 {args.year} 學年第 {args.term} 學期，共 {len(depts)} 個系所")
    raw = []
    for code, name in depts:
        try:
            raw += crawl_dept(code, name, args.year, args.term)
        except Exception as e:                       # 某個系失敗不要整個中斷
            print(f"  {name}（{code}）失敗：{e}")
        time.sleep(DELAY)

    os.makedirs("data", exist_ok=True)
    if not raw:
        raise SystemExit("沒有抓到任何資料，請檢查網址或學年學期是否正確。")
    pd.DataFrame(raw).to_csv("data/courses_raw.csv", index=False, encoding="utf-8-sig")
    courses = tidy(raw, args.term)
    courses.to_csv("data/courses.csv", index=False, encoding="utf-8-sig")
    print(f"完成：原始 {len(raw)} 列，整理後 {len(courses)} 門開課 → data/courses.csv")
