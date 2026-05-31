"""
国信证券开放 API 适配器。

国信开放 API 的具体委托 endpoint 与请求字段随获授权项目提供，本模块实现
官方公开的 AK/SK HMAC 签名、运行时配置、订单预检和显式开启后的真实提交。
"""
import base64
import hashlib
import hmac
import json
import os
import re
from email.utils import formatdate
from urllib.parse import quote, urljoin, urlparse

import requests


GUOSEN_DOC_URL = "https://openapi.guosen.com.cn/doc/"
GUOSEN_IQUANT_URL = "https://www.guosen.com.cn/gs/iquant/index.html"
LIVE_CONFIRMATION = "CONFIRM_LIVE_ORDER"


class BrokerError(ValueError):
    pass


class GuosenOpenAPIClient:
    """国信开放 API 的受控 HTTP 网关，不在浏览器或磁盘持久化密钥。"""

    def __init__(self):
        self.session = requests.Session()
        self.config = {
            "base_url": os.environ.get("GUOSEN_API_BASE_URL", "").rstrip("/"),
            "access_key": os.environ.get("GUOSEN_API_AK", ""),
            "secret_key": os.environ.get("GUOSEN_API_SK", ""),
            "account_id": os.environ.get("GUOSEN_ACCOUNT_ID", ""),
            "account_query_field": os.environ.get("GUOSEN_ACCOUNT_QUERY_FIELD", ""),
            "status_path": os.environ.get("GUOSEN_STATUS_PATH", ""),
            "account_path": os.environ.get("GUOSEN_ACCOUNT_PATH", ""),
            "positions_path": os.environ.get("GUOSEN_POSITIONS_PATH", ""),
            "trades_path": os.environ.get("GUOSEN_TRADES_PATH", ""),
            "orders_path": os.environ.get("GUOSEN_ORDERS_PATH", ""),
            "order_path": os.environ.get("GUOSEN_ORDER_PATH", ""),
            "order_template": os.environ.get("GUOSEN_ORDER_TEMPLATE", ""),
            "body_digest_required": os.environ.get("GUOSEN_BODY_DIGEST", "").upper() == "YES",
        }
        self.live_enabled = os.environ.get("GUOSEN_ENABLE_LIVE_TRADING", "").upper() == "YES"

    def configure(self, data: dict) -> dict:
        for field in (
            "base_url", "access_key", "secret_key", "account_id", "account_query_field",
            "status_path", "account_path", "positions_path", "trades_path",
            "orders_path", "order_path",
            "order_template",
        ):
            if field in data:
                value = str(data.get(field) or "").strip()
                if field == "base_url":
                    if value and urlparse(value).scheme != "https":
                        raise BrokerError("国信 API 地址必须使用 HTTPS")
                    value = value.rstrip("/")
                if field.endswith("_path") and value and not value.startswith("/"):
                    raise BrokerError(f"{field} 必须以 / 开头")
                self.config[field] = value
        if "body_digest_required" in data:
            self.config["body_digest_required"] = bool(data["body_digest_required"])
        return self.public_status()

    def public_status(self) -> dict:
        required = (
            self.config["base_url"], self.config["access_key"],
            self.config["secret_key"], self.config["order_path"],
            self.config["order_template"],
        )
        return {
            "provider": "国信证券开放 API",
            "integration_type": "AK/SK HMAC 网关",
            "configured": all(required),
            "base_url": self.config["base_url"],
            "has_access_key": bool(self.config["access_key"]),
            "has_secret_key": bool(self.config["secret_key"]),
            "account_id": self.config["account_id"],
            "account_query_field": self.config["account_query_field"],
            "status_path": self.config["status_path"],
            "account_path": self.config["account_path"],
            "positions_path": self.config["positions_path"],
            "trades_path": self.config["trades_path"],
            "orders_path": self.config["orders_path"],
            "order_path": self.config["order_path"],
            "has_order_template": bool(self.config["order_template"]),
            "body_digest_required": self.config["body_digest_required"],
            "live_enabled": self.live_enabled,
            "live_confirmation": LIVE_CONFIRMATION,
            "account_query_configured": all((
                self.config["base_url"], self.config["access_key"],
                self.config["secret_key"], self.config["account_path"],
                self.config["positions_path"],
            )),
            "trade_query_configured": all((
                self.config["base_url"], self.config["access_key"],
                self.config["secret_key"], self.config["trades_path"],
            )),
            "documents": [
                {"name": "国信开放API平台文档", "url": GUOSEN_DOC_URL},
                {"name": "国信iQuant产品页", "url": GUOSEN_IQUANT_URL},
            ],
            "notice": "具体委托接口路径及报文需以获授权项目的在线文档为准。",
        }

    @staticmethod
    def _canonical_query(params: dict = None) -> str:
        pairs = []
        for key, value in sorted((params or {}).items(), key=lambda pair: str(pair[0])):
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in values:
                pairs.append(
                    f"{quote(str(key), safe='-_.~')}={quote(str(item), safe='-_.~')}"
                )
        return "&".join(pairs)

    def signed_headers(self, method: str, path: str, params: dict = None,
                       body: bytes = b"", date_value: str = None) -> dict:
        access_key = self.config["access_key"]
        secret_key = self.config["secret_key"]
        if not access_key or not secret_key:
            raise BrokerError("请先配置国信 AK/SK")
        method = method.upper()
        date_value = date_value or formatdate(usegmt=True)
        query = self._canonical_query(params)
        signing_string = f"{method}\n{path}\n{query}\n{access_key}\n{date_value}\n"
        signature = base64.b64encode(
            hmac.new(secret_key.encode("utf-8"), signing_string.encode("utf-8"),
                     hashlib.sha256).digest()
        ).decode("ascii")
        headers = {
            "X-GS-API-AK": access_key,
            "X-GS-API-DATE": date_value,
            "X-GS-API-ALGORITHM": "hmac-sha256",
            "X-GS-API-SIGNATURE": signature,
            "Content-Type": "application/json",
        }
        if body and self.config["body_digest_required"]:
            headers["X-GS-API-BODY-DIGEST"] = base64.b64encode(
                hmac.new(secret_key.encode("utf-8"), body, hashlib.sha256).digest()
            ).decode("ascii")
        return headers

    def request(self, method: str, path: str, payload: dict = None,
                params: dict = None) -> dict:
        if not self.config["base_url"]:
            raise BrokerError("请配置国信授权项目 API 根地址")
        if not path:
            raise BrokerError("未配置所需接口路径")
        body = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if payload is not None else b""
        )
        headers = self.signed_headers(method, path, params=params, body=body)
        url = urljoin(self.config["base_url"] + "/", path.lstrip("/"))
        try:
            response = self.session.request(
                method.upper(), url, params=params, data=body or None,
                headers=headers, timeout=(5, 20)
            )
        except requests.RequestException as exc:
            raise BrokerError(f"国信 API 连接失败: {exc}") from exc
        try:
            content = response.json()
        except ValueError:
            content = {"text": response.text[:500]}
        return {"ok": response.ok, "status_code": response.status_code, "response": content}

    def test_connection(self) -> dict:
        path = self.config["status_path"] or self.config["account_path"]
        if not path:
            raise BrokerError("请配置联通测试路径或资金账户查询路径")
        params = None if self.config["status_path"] else self._account_params()
        return self.request("GET", path, params=params)

    def account(self) -> dict:
        return self.request("GET", self.config["account_path"], params=self._account_params())

    def positions(self) -> dict:
        return self.request("GET", self.config["positions_path"], params=self._account_params())

    def trades(self) -> dict:
        return self.request("GET", self.config["trades_path"], params=self._account_params())

    def orders(self) -> dict:
        return self.request("GET", self.config["orders_path"], params=self._account_params())

    def _account_params(self) -> dict:
        field = self.config["account_query_field"]
        account_id = self.config["account_id"]
        return {field: account_id} if field and account_id else None

    def preview_order(self, data: dict) -> dict:
        code = str(data.get("code", "")).strip()
        side = str(data.get("side", "")).upper()
        order_type = str(data.get("order_type", "LIMIT")).upper()
        try:
            price = float(data.get("price", 0))
            quantity = int(data.get("quantity", 0))
        except (ValueError, TypeError):
            raise BrokerError("价格和数量格式不正确")
        if not re.fullmatch(r"\d{6}", code):
            raise BrokerError("股票代码必须为6位数字")
        if side not in ("BUY", "SELL"):
            raise BrokerError("方向必须为 BUY 或 SELL")
        if order_type != "LIMIT":
            raise BrokerError("当前仅允许限价单，避免自动委托出现不可控成交价格")
        if price <= 0 or quantity <= 0:
            raise BrokerError("价格和数量必须大于0")
        if side == "BUY" and quantity % 100:
            raise BrokerError("A股买入数量必须为100股整数倍")

        market_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
        variables = {
            "account_id": self.config["account_id"],
            "code": code,
            "market_code": market_code,
            "side": side,
            "price": f"{price:.2f}",
            "quantity": str(quantity),
            "order_type": order_type,
        }
        template = self.config["order_template"]
        if not template:
            template = (
                '{"account":"{{account_id}}","symbol":"{{market_code}}",'
                '"side":"{{side}}","orderType":"{{order_type}}",'
                '"price":{{price}},"quantity":{{quantity}}}'
            )
        rendered = template
        for key, value in variables.items():
            rendered = rendered.replace("{{" + key + "}}", value)
        try:
            payload = json.loads(rendered)
        except json.JSONDecodeError as exc:
            raise BrokerError(f"订单 JSON 模板渲染失败: {exc.msg}") from exc
        warnings = [
            "真实下单前需在国信测试环境验证该接口路径和字段映射",
            "A股买入成交后当日不可卖出（T+1）",
        ]
        if not self.config["order_template"]:
            warnings.append("当前展示为示例报文；实盘提交前必须配置授权文档对应模板")
        return {
            "code": code,
            "side": side,
            "order_type": order_type,
            "price": round(price, 2),
            "quantity": quantity,
            "notional": round(price * quantity, 2),
            "payload": payload,
            "warnings": warnings,
        }

    def submit_order(self, data: dict) -> dict:
        preview = self.preview_order(data)
        if not self.live_enabled:
            raise BrokerError(
                "实盘下单未启用；服务端设置 GUOSEN_ENABLE_LIVE_TRADING=YES 后重启方可开放"
            )
        if str(data.get("confirmation", "")) != LIVE_CONFIRMATION:
            raise BrokerError("实盘确认口令不正确")
        if not self.public_status()["configured"]:
            raise BrokerError("国信 API 配置不完整，无法提交真实委托")
        submitted = self.request("POST", self.config["order_path"], preview["payload"])
        return {"preview": preview, "submitted": submitted}


guosen_client = GuosenOpenAPIClient()
