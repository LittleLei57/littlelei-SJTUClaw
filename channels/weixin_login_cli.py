"""Interactive QR login for the Tencent Weixin iLink channel."""

from pathlib import Path
import time

import qrcode

from channels.weixin import WeixinCredentialStore, WeixinLoginManager


def main():
    data_dir = Path(__file__).resolve().parents[1] / "data"
    manager = WeixinLoginManager(WeixinCredentialStore(data_dir))
    result = manager.start()
    url = result.get("qrcodeUrl")
    if not url:
        raise RuntimeError("微信服务没有返回二维码内容。")
    qr = qrcode.QRCode(border=1)
    qr.add_data(url); qr.make(fit=True); qr.print_ascii(invert=True)
    print("请使用手机微信扫描二维码并确认。")
    session_id = result["sessionId"]
    deadline = time.monotonic() + 5 * 60
    last_status = None
    while True:
        if time.monotonic() >= deadline:
            raise RuntimeError("微信扫码登录等待超过 5 分钟，请重新运行命令生成二维码。")
        status = manager.poll(session_id)
        name = status.get("status")
        if name != last_status:
            labels = {
                "wait":"等待扫码……", "scaned":"已扫码，请在手机上确认……",
                "scaned_but_redirect":"已扫码，正在切换微信服务节点……",
                "need_verifycode":"需要数字验证码。", "confirmed":"已确认。",
                "binded_redirect":"该微信机器人已绑定过其他本地实例。",
            }
            print(labels.get(name, f"微信登录状态：{name}"))
            last_status = name
        if name == "confirmed":
            print("微信连接成功。请重启 Gateway 使通道生效。")
            return
        if name == "need_verifycode":
            code = input("请输入手机微信显示的数字验证码：").strip()
            status = manager.poll(session_id, code)
            if status.get("status") == "confirmed":
                print("微信连接成功。请重启 Gateway 使通道生效。")
                return
        if name in {"expired", "verify_code_blocked"}:
            raise RuntimeError("二维码已失效或验证码被锁定，请重新运行登录命令。")
        if name == "binded_redirect":
            raise RuntimeError("该微信机器人已绑定，但本项目没有对应本地凭证；请在原绑定端解除后重新扫码。")
        time.sleep(1)


if __name__ == "__main__":
    main()
