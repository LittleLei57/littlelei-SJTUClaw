"""Tencent Weixin iLink QR login, long-poll channel, and text replies."""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
import secrets
from threading import Event, Thread
import time
import uuid
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

from channels.base import InboundMessage, OutboundEvent, format_notification_text
from channels.rich_text import prepare_channel_markdown
from channels.service import ChannelService


logger = logging.getLogger(__name__)
DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CLIENT_VERSION = (0 << 24) | (2 << 16) | (4 << 8) | 6
MAX_RECONNECT_DELAY_SECONDS = 60
PROCESSING_HINT_TIMEOUT_SECONDS = 3


class WeixinCredentialStore:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "weixin-account.json"

    def load(self):
        if not self.path.exists(): return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, value):
        temp = self.path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        temp.replace(self.path)
        try: os.chmod(self.path, 0o600)
        except OSError: pass


class WeixinAdapter:
    name = "weixin"

    def __init__(self, store: WeixinCredentialStore, opener=urlopen):
        self.store, self._opener = store, opener
        self.connection: WeixinConnection | None = None

    def send(self, message: InboundMessage, event: OutboundEvent):
        if event.event_type not in {"final", "assistant_note", "approval_required", "error"}: return
        if not self.connection: raise RuntimeError("微信通道未启动。")
        self.connection.send_text(
            message,
            prepare_channel_markdown(format_notification_text(event)),
        )

    def begin_processing(self, message: InboundMessage):
        if not self.connection:
            return None
        # iLink has no native typing action. Send the fallback *before* the
        # Agent turn starts, otherwise a slow concurrent send can arrive with
        # (or even after) the final answer and lose its purpose. Use a short
        # timeout: processing feedback is best-effort and must not delay work.
        try:
            self.connection.send_text(
                message,
                "··· 正在处理",
                timeout=PROCESSING_HINT_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.debug("微信处理提示发送失败：%s", exc)
        return None

    @staticmethod
    def end_processing(message: InboundMessage, handle) -> None:
        return None


class WeixinLoginManager:
    def __init__(self, store: WeixinCredentialStore, opener=urlopen):
        self.store, self._opener, self.sessions = store, opener, {}

    def start(self):
        payload = self._request("POST", DEFAULT_BASE_URL, "ilink/bot/get_bot_qrcode?bot_type=3", {"local_token_list": []}, auth=False)
        session_id = uuid.uuid4().hex
        self.sessions[session_id] = {"qrcode":payload["qrcode"], "baseUrl":DEFAULT_BASE_URL, "createdAt":time.time()}
        return {"sessionId":session_id, "qrcodeUrl":payload.get("qrcode_img_content"), "status":"wait"}

    def poll(self, session_id, verify_code=None):
        session = self.sessions.get(session_id)
        if not session: raise KeyError("微信登录会话不存在或已过期。")
        endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(session['qrcode'], safe='')}"
        if verify_code: endpoint += f"&verify_code={quote(verify_code, safe='')}"
        payload = self._request("GET", session["baseUrl"], endpoint, None, auth=False, timeout=40)
        if payload.get("status") == "scaned_but_redirect" and payload.get("redirect_host"):
            session["baseUrl"] = "https://" + payload["redirect_host"]
        if payload.get("status") == "confirmed":
            credentials = {
                "token":payload.get("bot_token"), "accountId":payload.get("ilink_bot_id"),
                "userId":payload.get("ilink_user_id"), "baseUrl":payload.get("baseurl") or session["baseUrl"],
                "savedAt":time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            if not credentials["token"] or not credentials["accountId"]:
                raise RuntimeError("微信确认登录后未返回完整凭证。")
            self.store.save(credentials); self.sessions.pop(session_id, None)
        return {k:v for k,v in payload.items() if k not in {"bot_token"}}

    def _request(self, method, base_url, endpoint, body, auth=False, timeout=20):
        return _request_json(self._opener, method, base_url, endpoint, body, None if not auth else "", timeout)


class WeixinConnection:
    def __init__(self, adapter: WeixinAdapter, service: ChannelService):
        self.adapter, self.service = adapter, service
        adapter.connection = self
        self._stop, self._thread = Event(), None
        self.connected = False
        self.last_error = None

    @property
    def configured(self): return bool(self.adapter.store.load().get("token"))
    @property
    def running(self): return bool(self._thread and self._thread.is_alive())

    def start(self):
        if not self.configured or self.running: return self.running
        self._stop.clear(); self._thread=Thread(target=self._run,name="weixin-longpoll",daemon=True); self._thread.start(); return True

    def stop(self): self._stop.set()

    def _run(self):
        sync_buf = ""
        consecutive_failures = 0
        while not self._stop.is_set():
            creds = self.adapter.store.load()
            try:
                payload = _request_json(self.adapter._opener,"POST",creds.get("baseUrl") or DEFAULT_BASE_URL,"ilink/bot/getupdates",{
                    "get_updates_buf":sync_buf,"base_info":{"channel_version":"1.0.0","bot_agent":"SJTUClaw/1.0"}
                },creds.get("token"),40)
                if payload.get("ret",0)!=0 or payload.get("errcode",0)!=0:
                    raise RuntimeError(payload.get("errmsg") or f"微信 API 错误 {payload.get('errcode') or payload.get('ret')}")
                if consecutive_failures:
                    logger.info("微信长轮询已恢复（此前连续失败 %s 次）。", consecutive_failures)
                consecutive_failures = 0
                self.connected=True; self.last_error=None
                sync_buf=payload.get("get_updates_buf") or sync_buf
                for msg in payload.get("msgs") or []: self._accept(msg)
            except Exception as exc:
                if self._stop.is_set():
                    break
                consecutive_failures += 1
                self.connected=False; self.last_error=str(exc)
                delay = _reconnect_delay(consecutive_failures)
                # 首次失败和长时间持续失败需要可见；中间相同错误降到 DEBUG，
                # 避免断网或电脑休眠时每两秒刷满 Gateway 终端。
                if consecutive_failures == 1 or consecutive_failures % 6 == 0:
                    logger.warning(
                        "微信长轮询失败（连续 %s 次，%ss 后重试）：%s",
                        consecutive_failures, delay, exc,
                    )
                else:
                    logger.debug(
                        "微信长轮询重试等待（连续 %s 次，%ss）：%s",
                        consecutive_failures, delay, exc,
                    )
                self._stop.wait(delay)

    def _accept(self, msg):
        if msg.get("message_type")==2: return
        texts=[str(i.get("text_item",{}).get("text") or "") for i in msg.get("item_list") or [] if i.get("type")==1]
        text="\n".join(x for x in texts if x).strip()
        if not text: return
        inbound=InboundMessage(
            channel="weixin",event_id=str(msg.get("message_id") or msg.get("client_id") or uuid.uuid4().hex),
            external_user_id=str(msg.get("from_user_id") or ""), conversation_id=str(msg.get("session_id") or msg.get("from_user_id") or ""),
            text=text,reply_token=str(msg.get("message_id") or ""),metadata={"contextToken":msg.get("context_token")},
        )
        Thread(target=self._process,args=(inbound,),name=f"weixin-{inbound.event_id}",daemon=True).start()

    def _process(self,inbound):
        try: self.service.handle(self.adapter,inbound)
        except Exception as exc:
            logger.exception("处理微信消息失败：%s",exc)
            try: self.adapter.send(inbound,OutboundEvent(
                "error", f"SJTUClaw 处理失败，请查看 Gateway 日志（事件 {inbound.event_id}）。"
            ))
            except Exception: pass

    def send_text(self,message,text,timeout=20):
        creds=self.adapter.store.load()
        body={"msg":{"from_user_id":"","to_user_id":message.external_user_id,"client_id":uuid.uuid4().hex,
            "message_type":2,"message_state":2,"item_list":[{"type":1,"text_item":{"text":text}}],
            "context_token":message.metadata.get("contextToken")},"base_info":{"channel_version":"1.0.0","bot_agent":"SJTUClaw/1.0"}}
        return _request_json(
            self.adapter._opener, "POST", creds.get("baseUrl") or DEFAULT_BASE_URL,
            "ilink/bot/sendmessage", body, creds.get("token"), timeout,
        )


def _request_json(opener, method, base_url, endpoint, body, token, timeout):
    url=urljoin(base_url.rstrip("/")+"/",endpoint)
    headers={"Content-Type":"application/json","iLink-App-Id":"bot","iLink-App-ClientVersion":str(CLIENT_VERSION)}
    if method == "POST":
        headers.update({"AuthorizationType":"ilink_bot_token",
                        "X-WECHAT-UIN":base64.b64encode(str(secrets.randbits(32)).encode()).decode()})
    if token: headers["Authorization"] = f"Bearer {token}"
    data=None if body is None else json.dumps(body,ensure_ascii=False).encode()
    request=Request(url,data=data,headers=headers,method=method)
    with opener(request,timeout=timeout) as response:
        payload=json.loads(response.read().decode("utf-8"))
    if not isinstance(payload,dict): raise RuntimeError("微信 API 返回格式错误。")
    return payload


def _reconnect_delay(consecutive_failures: int) -> int:
    """指数退避：2、4、8、16、32、60 秒，之后保持 60 秒。"""
    failures = max(1, int(consecutive_failures))
    return min(MAX_RECONNECT_DELAY_SECONDS, 2 ** failures)
