"""One-time local, signature-verified group-ID setup (not a persistent service)."""
import base64
import getpass
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path


def verified_groups(body, signature, secret):
    expected = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    if not signature or not hmac.compare_digest(signature, expected):
        raise ValueError("Invalid LINE signature")
    payload = json.loads(body)
    groups = []
    for event in payload.get("events", []):
        source, message = event.get("source", {}), event.get("message", {})
        if (source.get("type") == "group" and source.get("groupId")
                and message.get("type") == "text" and message.get("text") == "TStockNews 設定"):
            groups.append(source["groupId"])
    return groups


def main():
    secret = getpass.getpass("LINE Channel Secret（隱藏輸入）：").strip()
    if not secret:
        raise ValueError("Channel Secret required")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if self.path != "/webhook" or length <= 0 or length > 1_000_000:
                    self.send_error(400)
                    return
                body = self.rfile.read(length)
                groups = verified_groups(body, self.headers.get("X-Line-Signature", ""), secret)
            except (ValueError, json.JSONDecodeError):
                self.send_error(403)
                return
            for group in groups:
                path = Path("data/line-group-id.txt")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(group + "\n", encoding="utf-8")
                print("已核驗群組事件，Group ID 存到 data/line-group-id.txt；完成後按 Ctrl+C 關閉。", flush=True)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

    print("本機一次性 webhook：http://127.0.0.1:8765/webhook；需用 HTTPS tunnel 對外連接。")
    with HTTPServer(("127.0.0.1", 8765), Handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
