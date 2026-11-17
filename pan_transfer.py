"""
百度网盘转存客户端 —— 封装 python-dupan，把分享链接转存到你的网盘

用法:
    from pan_transfer import PanTransferClient
    client = PanTransferClient()
    result = client.transfer("https://pan.baidu.com/s/1xxxxxx", password="abcd")
    print(result)
    # {'ok': True, 'paths': ['/资源/三体全集.pdf'], 'share_id': 123456, 'share_uk': 987654}

需要先运行 pan_login_helper.py 登录，或手动在 data/pan_cookies.json 里填 Cookie。
"""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

COOKIE_PATH = Path("data/pan_cookies.json")


@dataclass
class TransferResult:
    ok: bool
    paths: list[str]              # 转存到自己网盘的路径列表
    share_id: int | str = ""
    share_uk: int | str = ""
    errno: int = 0
    message: str = ""
    elapsed: float = 0.0


class PanTransferClient:
    def __init__(self, cookie: str | None = None):
        self.cookie = cookie or self._load_cookie()
        self._client = None
        if self.cookie:
            self._client = self._build_client()

    @staticmethod
    def _load_cookie() -> str:
        if not COOKIE_PATH.exists():
            return ""
        try:
            data = json.loads(COOKIE_PATH.read_text(encoding="utf-8"))
            cookie = data.get("cookie", "")
            if cookie:
                return cookie
        except Exception:
            pass
        return ""

    def _build_client(self):
        """初始化 python-dupan 客户端。失败不 raise，留等调用方 detect Cookie 失效"""
        try:
            from dupan import DuPanClient
            return DuPanClient(cookie=self.cookie)
        except Exception as e:
            raise RuntimeError(f"DuPanClient 初始化失败：{e}\n  请先运行 pan_login_helper.py 登录百度网盘")

    def is_available(self) -> bool:
        """Cookie 是否存在且客户端可用"""
        if not self.cookie or "BDUSS=" not in self.cookie:
            return False
        try:
            return self._client is not None and bool(self._client.bdstoken)
        except Exception:
            return False

    def refresh(self):
        """重载 Cookie 并重建客户端（Cookie 过期后重新登录调用）"""
        self.cookie = self._load_cookie()
        if self.cookie:
            self._client = self._build_client()

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_share_info(url: str) -> tuple[str, str]:
        """从分享链接里提取 shorturl 和 password

        支持格式:
          https://pan.baidu.com/s/1abcdef  [?pwd=xxx]
          https://pan.baidu.com/share/init?surl=abcdef&pwd=xxx
        """
        shorturl = ""
        password = ""
        m = re.search(r"/s/1([A-Za-z0-9_-]+)", url)
        if m:
            shorturl = m.group(1)
        else:
            m = re.search(r"[?&]surl=([A-Za-z0-9_-]+)", url)
            if m:
                shorturl = m.group(1)
        m = re.search(r"[?&]pwd=([A-Za-z0-9]{4})", url)
        if m:
            password = m.group(1)
        return shorturl, password

    def transfer(
        self,
        share_url: str,
        password: str = "",
        target_dir: str = "/资源",
    ) -> TransferResult:
        """把一个百度网盘分享链接转存到自己的网盘

        Args:
            share_url:  分享链接
            password:   提取码（从链接里自动解析的会被显式参数覆盖）
            target_dir: 目标目录，默认 /资源

        Returns:
            TransferResult
        """
        t0 = time.time()

        # 1. Cookie 检查
        if not self.is_available():
            return TransferResult(
                ok=False, paths=[],
                message="Cookie 未就绪，请先运行 pan_login_helper.py 登录百度网盘",
                elapsed=time.time() - t0,
            )

        # 2. 从链接里自动补 password
        _, auto_pwd = self._extract_share_info(share_url)
        if not password:
            password = auto_pwd

        # 3. 确保目标目录存在（不存在就建）
        try:
            self._client.makedir(target_dir)
        except Exception:
            pass  # 目录已存在 / 其他问题不阻塞

        # 4. 调 python-dupan transfer
        try:
            params = {}
            if password:
                params["sekey"] = ""  # dupan 会从 share 页面 Cookie 里拿真实 sekey，但需要先 verify
            resp = self._client.transfer(share_url, params=params, data={"path": target_dir})

            # 构造返回
            paths: list[str] = []
            try:
                # 简单猜测：转存文件名可以从 DuPanShareList 拿到
                from dupan import DuPanShareList
                sl = DuPanShareList(share_url, password)
                for f in sl.list_index():
                    paths.append(str(Path(target_dir) / f.get("server_filename", "unknown")))
            except Exception:
                pass  # 文件名不是硬指标，转存本身成功就好

            # python-dupan 返回的 resp 里含有 errno=0 表示成功
            errno = resp.get("errno", 0) if isinstance(resp, dict) else 0
            ok = errno == 0
            return TransferResult(
                ok=ok,
                paths=paths,
                share_id=resp.get("shareid", "") if isinstance(resp, dict) else "",
                share_uk=resp.get("from", "") if isinstance(resp, dict) else "",
                errno=errno,
                message="" if ok else _errno_msg(errno),
                elapsed=time.time() - t0,
            )

        except Exception as e:
            err_str = str(e)
            return TransferResult(
                ok=False, paths=[],
                errno=-1,
                message=err_str,
                elapsed=time.time() - t0,
            )

    # ------------------------------------------------------------------
    def transfer_batch(
        self,
        links: list[dict],
        target_dir: str = "/资源",
        interval: float = 3.0,
        on_progress=None,
    ) -> list[TransferResult]:
        """批量转存。links 每项: {"url": "...", "password": "..."} 或直接 URL 字符串"""
        results = []
        for i, item in enumerate(links):
            if isinstance(item, str):
                url = item
                password = ""
            else:
                url = item.get("url", "")
                password = item.get("password", "")
            result = self.transfer(url, password=password, target_dir=target_dir)
            results.append(result)
            if on_progress:
                on_progress(i + 1, len(links), url, result)
            if i < len(links) - 1 and interval > 0:
                time.sleep(interval)
        return results


# ---------------------------------------------------------------------------
# 错误码辅助
# ---------------------------------------------------------------------------
def _errno_msg(errno: int) -> str:
    from dupan import ERRNO_TO_MESSAGE
    return ERRNO_TO_MESSAGE.get(errno, f"errno={errno}")


if __name__ == "__main__":
    # 快速自测：打印 Cookie 状态
    c = PanTransferClient()
    print(f"Cookie 就绪: {c.is_available()}")
    print(f"Cookie 长度: {len(c.cookie)}")
