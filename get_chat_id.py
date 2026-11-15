"""Узнать свой chat id: сначала напиши боту любое сообщение, потом запусти
    python get_chat_id.py <TOKEN_БОТА>
"""
import sys

import requests

token = sys.argv[1] if len(sys.argv) > 1 else input("Токен бота: ").strip()
data = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
         for u in data.get("result", []) if "message" in u}
if not chats:
    print("Сообщений не найдено. Напиши боту в Telegram что-нибудь (например /start) и запусти снова.")
for cid, name in chats.items():
    print(f"chat id: {cid}   ({name})")
