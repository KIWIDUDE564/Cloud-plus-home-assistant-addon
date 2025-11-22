import base64
import gzip
import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, Optional

import requests
from gmssl.sm4 import CryptSM4, SM4_DECRYPT, SM4_ENCRYPT

API_BASE = "https://www.smartgencloudplus.cn/yewu"
SM4_KEY_HEX = "7346346d54327455307a55366d4c3775"
SIGN_SECRET = "fsh@TRuZ4dvcp5uY"
DEFAULT_LANGUAGE = "en-US"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_TIMEOUT = 15
DEFAULT_POLL_INTERVAL = 30
DEFAULT_UPDATE_DATE = "20250321"
DEFAULT_COMPANY_ID = "1"
OPTIONS_PATH = "/data/options.json"
DEFAULT_TOKEN_FILE = "/home/solar-assistant/smartgen_tokens.json"


def md5_hex(value: str) -> str:
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    padding_len = block_size - (len(data) % block_size)
    return data + bytes([padding_len] * padding_len)


def pkcs7_unpad(data: bytes, block_size: int = 16) -> bytes:
    if not data:
        raise ValueError("Invalid padding: empty data")
    padding_len = data[-1]
    if padding_len < 1 or padding_len > block_size:
        raise ValueError("Invalid padding length")
    if data[-padding_len:] != bytes([padding_len] * padding_len):
        raise ValueError("Invalid padding bytes")
    return data[:-padding_len]


def sm4_encrypt(plaintext: str) -> str:
    crypt = CryptSM4()
    crypt.set_key(bytes.fromhex(SM4_KEY_HEX), SM4_ENCRYPT)
    padded = pkcs7_pad(plaintext.encode("utf-8"))
    encrypted = crypt.crypt_ecb(padded)
    return base64.b64encode(encrypted).decode("utf-8")


def sm4_decrypt(ciphertext_b64: str) -> str:
    crypt = CryptSM4()
    crypt.set_key(bytes.fromhex(SM4_KEY_HEX), SM4_DECRYPT)
    cipher_bytes = base64.b64decode(ciphertext_b64)
    decrypted_padded = crypt.crypt_ecb(cipher_bytes)
    return pkcs7_unpad(decrypted_padded).decode("utf-8")


def make_x_sign(timestamp: int, token: Optional[str], *, login: bool = False) -> str:
    token_value = token or ""
    if token_value:
        inner = md5_hex(f"{token_value}{timestamp}{SIGN_SECRET}")
        return md5_hex(f"{token_value}{timestamp}{inner}")
    if login:
        inner = md5_hex(f"{timestamp}{SIGN_SECRET}")
        return md5_hex(f"{timestamp}{inner}")
    return md5_hex(f"{timestamp}{SIGN_SECRET}")


class SmartGenCloudBridge:
    def __init__(self, config: Dict[str, Any]):
        self.base_url = (config.get("api_base") or API_BASE).rstrip("/")
        self.token = config.get("token") or ""
        self.utoken = config.get("utoken") or ""
        self.username = config.get("username") or ""
        self.password = config.get("password") or ""
        self.company_id = str(config.get("company_id", DEFAULT_COMPANY_ID))
        self.language = config.get("language", DEFAULT_LANGUAGE)
        self.timezone = config.get("timezone", DEFAULT_TIMEZONE)
        self.timeout = int(config.get("timeout", DEFAULT_TIMEOUT))
        self.poll_interval = int(config.get("poll_interval", DEFAULT_POLL_INTERVAL))
        self.update_date = config.get("update_date", DEFAULT_UPDATE_DATE)
        self.token_file = config.get("token_file", DEFAULT_TOKEN_FILE)

        self.load_tokens()

    def load_tokens(self) -> None:
        if os.path.exists(self.token_file):
            try:
                with open(self.token_file, "r", encoding="utf-8") as token_file:
                    data = json.load(token_file)
                    self.token = data.get("token", self.token)
                    self.utoken = data.get("utoken", self.utoken)
            except Exception as err:  # noqa: BLE001
                logging.warning("Failed to load tokens from %s: %s", self.token_file, err)

    def save_tokens(self) -> None:
        payload = {
            "token": self.token,
            "utoken": self.utoken,
            "timestamp": int(time.time()),
        }
        try:
            os.makedirs(os.path.dirname(self.token_file), exist_ok=True)
            with open(self.token_file, "w", encoding="utf-8") as token_file:
                json.dump(payload, token_file, indent=2)
        except Exception as err:  # noqa: BLE001
            logging.error("Failed to save tokens to %s: %s", self.token_file, err)

    def _build_headers(self, token: str, timestamp: int, x_sign: str) -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": self.language,
            "User-Agent": "okhttp/4.9.0",
            "X-Time": str(timestamp),
            "X-Timezone": self.timezone,
            "X-UpdateDate": self.update_date,
            "X-Companyid": self.company_id,
            "X-Token": token,
            "X-Sign": x_sign,
            "Referer": "https://www.smartgencloudplus.cn/index",
            "Content-Type": "text/plain;charset=UTF-8",
        }

    def _encrypt_payload(self, payload: Dict[str, Any]) -> str:
        payload_json = json.dumps(payload, separators=(",", ":"))
        return sm4_encrypt(payload_json)

    def _decode_response(self, response: requests.Response) -> Dict[str, Any]:
        raw_bytes = response.content or b""
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            try:
                raw_bytes = gzip.decompress(raw_bytes)
            except OSError:
                pass
        try:
            body_text = raw_bytes.decode(response.encoding or "utf-8")
        except Exception:
            body_text = raw_bytes.decode("utf-8", errors="ignore")

        try:
            decrypted_text = sm4_decrypt(body_text)
            text_to_parse = decrypted_text
        except Exception:
            text_to_parse = body_text

        try:
            return json.loads(text_to_parse)
        except Exception:
            logging.debug("Response not JSON decodable, returning raw text")
            return {"raw": text_to_parse}

    def _post_encrypted(self, endpoint: str, payload: Dict[str, Any], *, token: Optional[str] = None, login: bool = False) -> Dict[str, Any]:
        url_path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        url = f"{self.base_url}{url_path}"
        timestamp = int(time.time())
        token_value = token if token is not None else self.token
        x_sign = make_x_sign(timestamp, token_value, login=login)
        headers = self._build_headers(token_value, timestamp, x_sign)
        encrypted_payload = self._encrypt_payload(payload)
        logging.debug("POST %s headers=%s", url, {k: v for k, v in headers.items() if k != "X-Token"})
        response = requests.post(url, data=encrypted_payload, headers=headers, timeout=self.timeout)

        if not login and response.status_code == 401 and self._auto_refresh_token():
            return self._post_encrypted(endpoint, payload, token=self.token, login=login)

        body_text = (response.text or "").lower()
        if not login and "token" in body_text and "invalid" in body_text and self._auto_refresh_token():
            return self._post_encrypted(endpoint, payload, token=self.token, login=login)

        response.raise_for_status()
        return self._decode_response(response)

    def _authorized_payload(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        base_payload: Dict[str, Any] = {
            "token": self.token,
            "utoken": self.utoken,
            "language": self.language,
            "timezone": self.timezone,
        }
        if extra:
            base_payload.update(extra)
        return base_payload

    def _update_tokens(self, token: Optional[str], utoken: Optional[str]) -> None:
        if token:
            self.token = token
        if utoken is not None:
            self.utoken = utoken
        self.save_tokens()

    def login(self, username: Optional[str] = None, password: Optional[str] = None) -> Dict[str, Any]:
        user = username or self.username
        passwd = password or self.password
        if not user or not passwd:
            raise ValueError("Username and password are required for login")

        payload = {"username": user, "password": passwd}
        response = self._post_encrypted("/user/login", payload, token="", login=True)
        data = response.get("data") if isinstance(response, dict) else None
        if isinstance(data, dict):
            self._update_tokens(data.get("token") or data.get("accessToken"), data.get("utoken") or data.get("refreshToken"))
        return response

    def _auto_refresh_token(self) -> bool:
        if not self.username or not self.password:
            return False
        try:
            logging.info("Refreshing SmartGen tokens via login")
            self.login(self.username, self.password)
            return True
        except Exception as err:  # noqa: BLE001
            logging.error("Token refresh failed: %s", err)
            return False

    def user_info(self) -> Dict[str, Any]:
        return self._post_encrypted("/user/info", self._authorized_payload())

    def get_route(self) -> Dict[str, Any]:
        return self._post_encrypted("/getRoute", self._authorized_payload())

    def get_alarm_list(self, page: int = 1, page_size: int = 50) -> Dict[str, Any]:
        return self._post_encrypted(
            "/getalarmList",
            self._authorized_payload({"page": page, "pageSize": page_size}),
        )

    def get_running_time(self) -> Dict[str, Any]:
        return self._post_encrypted("/getRunningtime", self._authorized_payload())

    def get_pie_chart(self) -> Dict[str, Any]:
        return self._post_encrypted("/getPiechart", self._authorized_payload())

    def get_ranking_list(self) -> Dict[str, Any]:
        return self._post_encrypted("/getRankingList", self._authorized_payload())

    def get_monitor_list(self) -> Dict[str, Any]:
        return self._post_encrypted("/realTimeData/monitorList", self._authorized_payload())

    def run(self) -> None:
        logging.info("Starting SmartGen Cloud Bridge polling loop")
        if not self.token and self.username and self.password:
            try:
                self.login(self.username, self.password)
            except Exception as err:  # noqa: BLE001
                logging.error("Initial login failed: %s", err)
        while True:
            try:
                monitor = self.get_monitor_list()
                logging.info("Monitor list response received")
                logging.debug("Monitor payload: %s", monitor)
            except Exception as err:
                logging.error("Monitor list fetch failed: %s", err)
            time.sleep(self.poll_interval)


def load_config(path: str = OPTIONS_PATH) -> Dict[str, Any]:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as file:
                return json.load(file)
        except Exception as err:  # noqa: BLE001
            logging.error("Failed to load config: %s", err)
    return {}


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )


def self_test() -> None:
    sample = {"hello": "world"}
    sample_text = json.dumps(sample, separators=(",", ":"))
    encrypted = sm4_encrypt(sample_text)
    decrypted = sm4_decrypt(encrypted)
    logging.info("SM4 self-test decrypted payload: %s", decrypted)
    timestamp = int(time.time())
    logging.info("X-Sign (with token) sample: %s", make_x_sign(timestamp, "token"))
    logging.info("X-Sign (login) sample: %s", make_x_sign(timestamp, None, login=True))


def main() -> None:
    config = load_config()
    configure_logging(config.get("log_level", "info"))
    self_test()
    bridge = SmartGenCloudBridge(config)
    try:
        bridge.run()
    except KeyboardInterrupt:
        logging.info("SmartGen Cloud Bridge stopped")


if __name__ == "__main__":
    main()
