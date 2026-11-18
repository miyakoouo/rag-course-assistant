import os
import sys
import time
import pandas as pd
import chromadb
from dotenv import load_dotenv
from google import genai
from google.genai import types, errors

# ===== 設定區 =====
load_dotenv()
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
db = chromadb.PersistentClient(path="chroma_db")  # 知識庫存在 chroma_db 資料夾

CHAT_MODEL = "gemini-3.8-flash"         # 負責回答的模型
EMBED_MODEL = "gemini-embedding-001"    # 負責轉向量的模型
CHUNK_SIZE = 300                        # 規定文字每段最多幾個字（階段 6 會改這個值做實驗）
TOP_K = 4                               # 每次檢索找幾段資料


# ===== 第 1 部分：讀資料、切段 =====
def load_chunks():
    """回傳一個清單，每個元素是 (編號, 文字, 來源)"""
    chunks = []

    # 每門課組成一段完整的文字（courses.csv 由 crawler.py 產生）
    df = pd.read_csv("data/courses.csv", dtype=str, encoding="utf-8-sig").fillna("")
    for _, row in df.iterrows():
        text = (
            f"課程：{row['name']}（課號 {row['code']}），由{row['dept']}開課，"
            f"班級：{row['class_name']}，{row['credits']} 學分，{row['type']}，"
            f"{row['semester']}學期，授課教師：{row['teacher']}，"
            f"上課時間：{row['schedule']}，教室：{row['room'] or '未定'}。"
        )
        if row["program"]:
            text += f"屬於學程：{row['program']}。"
        if row["description"]:
            text += f"簡介：{row['description']}"
        chunks.append((f"course-{row['course_id']}", text, f"courses.csv：{row['dept']} {row['name']}"))

    # 修業規定依空行分段，太長的段落再依 CHUNK_SIZE 切開
    with open("data/rules.txt", encoding="utf-8-sig") as f:
        paragraphs = [p.strip() for p in f.read().split("\n\n") if p.strip()]
    n = 0
    for p in paragraphs:
        for start in range(0, len(p), CHUNK_SIZE):
            title = p.split("\n")[0] if p.startswith("【") else ""
            chunks.append((f"rule-{n}", p[start:start + CHUNK_SIZE], f"rules.txt：{title}"))
            n += 1

    return chunks


# ===== 呼叫 Gemini（遇到忙碌會自動重試）=====
def with_retry(call, retries=4):
    """伺服器忙碌（503）或超過每分鐘額度（429）時，等一下再試，等待時間每次加倍"""
    wait = 5
    for i in range(retries):
        try:
            return call()
        except errors.APIError as e:
            if e.code in (429, 503) and i < retries - 1:
                print(f"（Gemini 忙碌中，{wait} 秒後自動重試…）")
                time.sleep(wait)
                wait *= 2
            else:
                raise


def generate(prompt):
    return with_retry(lambda: client.models.generate_content(model=CHAT_MODEL, contents=prompt).text)


# ===== 第 2 部分：文字轉向量 =====
def embed(texts, task):
    """task：存文件用 RETRIEVAL_DOCUMENT，查詢問題用 RETRIEVAL_QUERY"""
    vectors = []
    for i in range(0, len(texts), 50):  # 一次送 50 段，避免超過 API 限制
        result = with_retry(lambda: client.models.embed_content(
            model=EMBED_MODEL,
            contents=texts[i:i + 50],
            config=types.EmbedContentConfig(task_type=task),
        ))
        vectors += [e.values for e in result.embeddings]
    return vectors


# ===== 第 3 部分：建立知識庫 =====
def build_index():
    chunks = load_chunks()
    try:
        db.delete_collection("courses")  # 先刪掉舊的，避免重複
    except Exception:
        pass
    collection = db.create_collection("courses")
    texts = [c[1] for c in chunks]
    collection.add(
        ids=[c[0] for c in chunks],
        documents=texts,
        embeddings=embed(texts, "RETRIEVAL_DOCUMENT"),
        metadatas=[{"source": c[2]} for c in chunks],
    )
    print(f"知識庫建立完成，共 {len(chunks)} 段資料")


# ===== 第 4 部分：檢索 =====
def search(question, k=TOP_K):
    """回傳 [(資料文字, 來源), ...]，越前面越相關"""
    collection = db.get_collection("courses")
    result = collection.query(
        query_embeddings=embed([question], "RETRIEVAL_QUERY"),
        n_results=k,
    )
    docs = result["documents"][0]
    sources = [m["source"] for m in result["metadatas"][0]]
    return list(zip(docs, sources))


# ===== 第 5 部分：交給 LLM 回答 =====
def answer(question):
    hits = search(question)
    context = "\n\n".join(
        f"[資料{i + 1}]（來源：{src}）\n{doc}" for i, (doc, src) in enumerate(hits)
    )
    prompt = f"""你是大學資管系的選課助理。請「只根據」下面的資料回答學生的問題。
規則：
1. 資料裡沒有的內容，回答「目前資料中找不到相關資訊，建議詢問系辦」，不可以自己編造。
2. 使用繁體中文，語氣親切，回答精簡。
3. 每個重點句尾標註引用的資料編號，例如 [資料1]。

{context}

學生問題：{question}"""
    return generate(prompt), hits


# ===== 在終端機測試用 =====
if __name__ == "__main__":
    if "--build" in sys.argv:
        build_index()
    while True:
        q = input("\n請輸入問題（輸入 q 離開）：").strip()
        if q == "q":
            break
        try:
            text, hits = answer(q)
        except Exception as e:  # 重試後還是失敗：顯示原因，但不要讓程式整個結束
            print(f"\n暫時無法回答，請稍後再試。（{e}）")
            continue
        print("\n" + text)
        print("\n--- 參考資料 ---")
        for doc, src in hits:
            print("•", src)
