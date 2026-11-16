import os
from dotenv import load_dotenv
from google import genai

load_dotenv()  # 讀取 .env 裡的 API Key
client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

response = client.models.generate_content(
    model="gemini-3.8-flash",
    contents="用一句話介紹你自己",
)
print(response.text)