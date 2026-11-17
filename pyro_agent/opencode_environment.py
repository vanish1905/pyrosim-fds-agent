"""Build a child-only OpenCode environment from standard proxy settings."""
from __future__ import annotations

import os
import sys


def _windows_proxy() -> tuple[str | None, str | None]:
    if sys.platform != "win32":
        return None, None
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
        if not int(enabled):
            return None, None
    except (ImportError, OSError, TypeError, ValueError):
        return None, None

    proxy_server = str(proxy_server).strip()
    if not proxy_server or "://" in proxy_server and not proxy_server.lower().startswith(("http://", "https://")):
        return None, None
    entries: dict[str, str] = {}
    if "=" in proxy_server:
        for part in proxy_server.split(";"):
            name, separator, value = part.partition("=")
            if separator and value.strip():
                entries[name.strip().lower()] = value.strip()
        http_proxy = entries.get("http") or entries.get("https")
        https_proxy = entries.get("https") or entries.get("http")
    else:
        # Windows commonly stores a single host:port for all protocols.
        http_proxy = https_proxy = proxy_server

    def normalize(value: str | None) -> str | None:
        if not value:
            return None
        value = value.strip()
        if "://" not in value:
            value = "http://" + value
        if not value.lower().startswith(("http://", "https://")):
            return None
        # Do not lift credentials from Windows' proxy registry value into a
        # child environment. Authenticated proxies must be configured explicitly.
        if "@" in value.split("://", 1)[1].split("/", 1)[0]:
            return None
        return value

    return normalize(http_proxy), normalize(https_proxy)


def configure_opencode_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """Preserve explicit proxy env; otherwise translate Windows' static proxy.

    OpenCode documents HTTP_PROXY/HTTPS_PROXY and NO_PROXY, but its Windows
    CLI does not automatically consume the WinINET proxy registry setting.
    Only the spawned process is changed; no user or machine setting is written.
    """
    result = dict(os.environ if env is None else env)
    has_http = bool(result.get("HTTP_PROXY") or result.get("http_proxy"))
    has_https = bool(result.get("HTTPS_PROXY") or result.get("https_proxy"))
    if not (has_http and has_https):
        http_proxy, https_proxy = _windows_proxy()
        if not has_http and http_proxy:
            result["HTTP_PROXY"] = http_proxy
        if not has_https and https_proxy:
            result["HTTPS_PROXY"] = https_proxy

    no_proxy_key = "NO_PROXY" if "NO_PROXY" in result or "no_proxy" not in result else "no_proxy"
    no_proxy = [item.strip() for item in result.get(no_proxy_key, "").split(",") if item.strip()]
    present = {item.casefold() for item in no_proxy}
    for host in ("localhost", "127.0.0.1", "::1"):
        if host.casefold() not in present:
            no_proxy.append(host)
    result[no_proxy_key] = ",".join(no_proxy)
    return result

