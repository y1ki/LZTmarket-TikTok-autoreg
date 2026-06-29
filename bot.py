import asyncio
import imaplib
import email as email_lib
import os
import json
import random
import re
import string
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Any, Optional
from loguru import logger
from playwright.async_api import async_playwright, Page
from playwright_stealth import stealth_async, StealthConfig
import ssl

ssl._create_default_https_context = ssl._create_unverified_context
class InvalidMailError(Exception):
    """Неверный логин или пароль от почты."""
    pass


lzt_key = "y1ki"  # токен маркета

price        = 1   # Цена лота в рублях
count        = 1   # Сколько аккаунтов регистрировать за один цикл
count_window = 1   # Сколько окон (потоков) открывать параллельно
break_time   = 1   # Перерыв между циклами в МИНУТАХ


logger.remove()
logger.add(
    lambda msg: print(msg, end=""),
    level="WARNING",
    colorize=True,
    format="<level>{message}</level>",
)
logger.add(
    "registration.log",
    level="WARNING",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
)


@dataclass
class Config:
    sadcaptcha_api_key: str = "23b91c44c9735cb336aaf2ff46335d48"
    lzt_token: str = ""

    accounts_filename: str = "acc.txt"
    proxies_filename: str = "proxies.txt"
    mails_filename: str = "mails.txt"

    max_browsers: int = 10
    browser_headless: bool = False

    page_load_timeout: int = 30
    captcha_check_timeout: int = 60
    action_delay: float = 0.2

    delay_min: int = 60
    delay_max: int = 180
    threads: int = 1

    browser_args: List[str] = field(
        default_factory=lambda: [
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-setuid-sandbox",
            "--disable-infobars",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-site-isolation-trials",
            "--ignore-certificate-errors",
            "--disable-accelerated-2d-canvas",
            "--disable-browser-side-navigation",
            "--disable-default-apps",
            "--no-first-run",
        ]
    )

    browser_context_options: Dict[str, Any] = field(
        default_factory=lambda: {
            "viewport": {"width": 1920, "height": 1080},
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
            "ignore_https_errors": True,
            "java_script_enabled": True,
        }
    )

    stealth_config: Dict[str, bool] = field(
        default_factory=lambda: {
            "navigator_languages": False,
            "navigator_vendor": False,
            "navigator_user_agent": False,
        }
    )


class MailManager:
    """Менеджер почт из файла mails.txt (формат: login;password — почта login@rambler.ru)"""

    IMAP_HOST = "imap.rambler.ru"
    IMAP_PORT = 993

    def __init__(self, filename: str):
        self.filename = filename
        self._lock = None

    def _get_lock(self):
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def get_mail(self) -> Optional[Dict[str, str]]:
        """Берет первую почту из файла и удаляет её из файла."""
        async with self._get_lock():
            try:
                abs_path = os.path.abspath(self.filename)
                print(f"[MAIL] Файл почт: {abs_path}")

                with open(abs_path, "r", encoding="utf-8") as f:
                    raw_lines = f.readlines()

                lines = [l.strip() for l in raw_lines if l.strip() and not l.strip().startswith("#")]

                print(f"[MAIL] Найдено почт в файле: {len(lines)}")

                if not lines:
                    print("[MAIL] Файл mails.txt пуст — добавьте почты!")
                    return None

                first_line = lines[0]
                remaining = lines[1:]

                with open(abs_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(remaining) + ("\n" if remaining else ""))

                print(f"[MAIL] Взята почта: {first_line.split(';')[0]}  |  Осталось в файле: {len(remaining)}")

                parts = first_line.split(";")
                if len(parts) < 2:
                    print(f"[MAIL] Неверный формат строки: {first_line}  (нужно login;password)")
                    return None

                raw_login = parts[0].strip()
                mail_pass = parts[1].strip()

                if "@" in raw_login:
                    mail_email = raw_login
                else:
                    mail_email = f"{raw_login}@rambler.ru"

                return {
                    "login": mail_email,
                    "password": mail_pass,
                    "email": mail_email,
                }

            except FileNotFoundError:
                print(f"[MAIL] Файл не найден: {os.path.abspath(self.filename)}")
                return None
            except Exception as e:
                print(f"[MAIL] Ошибка чтения файла: {e}")
                return None

    async def get_verification_code(self, mail_login: str, mail_password: str) -> Optional[str]:
        """Получает код подтверждения из rambler.ru через IMAP."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, self._fetch_code_imap, mail_login, mail_password
        )
        if result == "INVALID_CREDENTIALS":
            raise InvalidMailError(f"Неверный логин/пароль: {mail_login}")
        return result

    def _fetch_code_imap(self, mail_login: str, mail_password: str) -> Optional[str]:
        """Синхронная функция получения кода через IMAP."""
        mail_email = mail_login
        for attempt in range(24):
            try:
                logger.info(f"IMAP попытка {attempt + 1}/24...")
                mail = imaplib.IMAP4_SSL(self.IMAP_HOST, self.IMAP_PORT)
                try:
                    mail.login(mail_email, mail_password)
                except imaplib.IMAP4.error as login_err:
                    err_str = str(login_err).lower()
                    if "invalid login" in err_str or "invalid password" in err_str or "authentication failed" in err_str:
                        logger.error(f"[MAIL] Неверный логин/пароль для {mail_email} — пропускаем почту")
                        return "INVALID_CREDENTIALS"
                    raise
                mail.select("INBOX")

                status, messages = mail.search(None, "UNSEEN")
                if status != "OK":
                    mail.logout()
                    time.sleep(5)
                    continue

                mail_ids = messages[0].split()
                if not mail_ids:
                    mail.logout()
                    time.sleep(5)
                    continue

                for mail_id in reversed(mail_ids):
                    status, msg_data = mail.fetch(mail_id, "(RFC822)")
                    if status != "OK":
                        continue

                    raw = msg_data[0][1]
                    msg = email_lib.message_from_bytes(raw)

                    subject = msg.get("Subject", "").lower()
                    sender = msg.get("From", "").lower()

                    if not any(
                        kw in subject + sender
                        for kw in ["tiktok", "verification", "verify", "noreply", "код", "подтверждение"]
                    ):
                        continue

                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            ct = part.get_content_type()
                            if ct in ("text/plain", "text/html"):
                                try:
                                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                except Exception:
                                    pass
                    else:
                        try:
                            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
                        except Exception:
                            pass

                    matches = re.findall(r"\b(\d{6})\b", body)
                    for match in matches:
                        if match.isdigit() and len(match) == 6:
                            mail.logout()
                            logger.success(f"Код найден: {match}")
                            return match

                mail.logout()
            except Exception as e:
                logger.error(f"Ошибка IMAP (попытка {attempt + 1}): {e}")

            time.sleep(5)

        logger.warning("Код не получен за 2 минуты ожидания")
        return None


class CaptchaSolver:
    def __init__(self, page, api_key, config, **kwargs):
        self.page = page
        self.api_key = api_key
        self.config = config

    async def solve_captcha_if_present(self):
        try:
            await asyncio.sleep(2)

            recaptcha_frame = await self.page.query_selector('iframe[src*="recaptcha"]')
            if recaptcha_frame:
                await self._handle_recaptcha_v2()

            hcaptcha_frame = await self.page.query_selector('iframe[src*="hcaptcha"]')
            if hcaptcha_frame:
                await self._handle_hcaptcha()

            generic_captcha = await self.page.query_selector(
                '.captcha, [data-testid="captcha"], div[class*="captcha"]'
            )
            if generic_captcha:
                await self._handle_generic_captcha()

            await asyncio.sleep(3)

        except Exception as e:
            logger.debug(f"Ошибка при обработке капчи: {e}")

    async def _handle_recaptcha_v2(self):
        try:
            await asyncio.sleep(2)
            if self.api_key and self.api_key != "SADCAPTCHA_API_KEY":
                await asyncio.sleep(5)
            else:
                logger.warning("Обнаружена reCAPTCHA! Решите вручную...")
                for _ in range(self.config.captcha_check_timeout):
                    await asyncio.sleep(1)
                    if not await self.page.query_selector('iframe[src*="recaptcha"]'):
                        break
        except Exception as e:
            logger.error(f"Ошибка обработки reCAPTCHA: {e}")

    async def _handle_hcaptcha(self):
        try:
            if self.api_key and self.api_key != "SADCAPTCHA_API_KEY":
                await asyncio.sleep(5)
            else:
                logger.warning("Обнаружена hCaptcha! Решите вручную...")
                for _ in range(self.config.captcha_check_timeout):
                    await asyncio.sleep(1)
                    if not await self.page.query_selector('iframe[src*="hcaptcha"]'):
                        break
        except Exception as e:
            logger.error(f"Ошибка обработки hCaptcha: {e}")

    async def _handle_generic_captcha(self):
        try:
            logger.warning("Обнаружена капча! Решите вручную...")
            for _ in range(self.config.captcha_check_timeout // 2):
                await asyncio.sleep(1)
                if not await self.page.query_selector(
                    '.captcha, [data-testid="captcha"], div[class*="captcha"]'
                ):
                    break
        except Exception as e:
            logger.error(f"Ошибка обработки капчи: {e}")


async def _click_skip_button(page) -> bool:
    """
    Ищет и кликает кнопку 'Пропустить' / 'Skip' на странице TikTok.
    Использует XPath через JS — работает с любым типом элемента (div, a, span, button).
    Возвращает True если кнопка найдена и нажата.
    """
    for text in ["Пропустить", "Skip"]:
        try:
            clicked = await page.evaluate(f"""
                () => {{
                    const xpath = "//*[normalize-space(text())='{text}' or normalize-space(.)='{text}']";
                    const result = document.evaluate(
                        xpath, document, null,
                        XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null
                    );
                    for (let i = 0; i < result.snapshotLength; i++) {{
                        const el = result.snapshotItem(i);
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {{
                            el.click();
                            return true;
                        }}
                    }}
                    return false;
                }}
            """)
            if clicked:
                logger.success(f"Нажат элемент 'Пропустить' (JS/XPath)")
                return True
        except Exception:
            pass

    for text in ["Пропустить", "Skip"]:
        try:
            loc = page.get_by_text(text, exact=True)
            if await loc.count() > 0:
                await loc.first.click()
                logger.success(f"Нажат элемент '{text}' (locator)")
                return True
        except Exception:
            pass

    return False


def load_config() -> Config:
    config = Config()
    config.lzt_token = lzt_key

    if os.path.exists("config.json"):
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                config_data = json.load(f)
                for key, value in config_data.items():
                    if hasattr(config, key) and not key.startswith("_"):
                        setattr(config, key, value)
        except Exception as e:
            logger.warning(f"Ошибка загрузки config.json: {e}")

    config.sadcaptcha_api_key = os.getenv("SADCAPTCHA_API_KEY", config.sadcaptcha_api_key)
    return config


class ProxyManager:
    def __init__(self, config: Config):
        self.proxies = []
        self.current_index = 0
        self.load_proxies(config.proxies_filename)

    def load_proxies(self, filename: str):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and ":" in line:
                        parts = line.split(":")
                        if len(parts) == 4:
                            ip, port, username, password = parts
                            self.proxies.append({
                                "server": f"http://{ip}:{port}",
                                "username": username,
                                "password": password,
                            })
        except FileNotFoundError:
            logger.error(f"Файл {filename} не найден!")
        except Exception as e:
            logger.error(f"Ошибка загрузки прокси: {e}")

    def get_next_proxy(self) -> Optional[Dict]:
        if not self.proxies:
            return None
        proxy = self.proxies[self.current_index]
        self.current_index = (self.current_index + 1) % len(self.proxies)
        return proxy


class DataGenerator:
    @staticmethod
    def generate_password() -> str:
        uppercase = random.choice(string.ascii_uppercase)
        lowercase = "".join(random.choices(string.ascii_lowercase, k=5))
        digits = "".join(random.choices(string.digits, k=4))
        return uppercase + lowercase + "_" + digits

    @staticmethod
    def generate_birth_date() -> Dict[str, str]:
        current_year = datetime.now().year
        birth_year = random.randint(current_year - 25, current_year - 18)
        birth_month = random.randint(1, 12)
        if birth_month in [1, 3, 5, 7, 8, 10, 12]:
            max_day = 31
        elif birth_month in [4, 6, 9, 11]:
            max_day = 30
        else:
            max_day = 28 if birth_year % 4 != 0 else 29
        birth_day = random.randint(1, max_day)
        return {
            "day": str(birth_day),
            "month": str(birth_month),
            "year": str(birth_year),
        }

    @staticmethod
    def get_random_user_agent() -> str:
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:119.0) Gecko/20100101 Firefox/119.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ]
        return random.choice(user_agents)


class LZTMarket:
    BASE_URL = "https://prod-api.lzt.market"

    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    def check_token(self) -> bool:
        import urllib.request

        req = urllib.request.Request(
            f"{self.BASE_URL}/me",
            headers=self.headers,
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                username = (data.get("user") or {}).get("username", "")
                if username:
                    logger.success(f"LZT Market токен рабочий! Аккаунт: {username}")
                    return True
                logger.error("Токен не прошёл проверку")
                return False
        except Exception as e:
            logger.error(f"Ошибка проверки токена: {e}")
            return False

    def _post_safe(self, endpoint: str, data: dict) -> dict:
        import urllib.request
        import urllib.parse
        import urllib.error

        encoded = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request(
            f"{self.BASE_URL}/{endpoint}",
            data=encoded,
            headers=self.headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8")
                parsed = json.loads(body)
            except Exception:
                parsed = {"error": f"HTTP {e.code}"}
            return parsed
        except Exception as e:
            return {"error": str(e)}

    def _check_account(self, item_id: int, cookies_json: str = "", login_password: str = "") -> bool:
        body = {}
        if cookies_json:
            body["extra[cookies]"] = cookies_json
        if login_password:
            body["login_password"] = login_password

        for attempt in range(1, 101):
            result = self._post_safe(f"{item_id}/goods/check", body)

            if result.get("status") == "ok" or "item" in result:
                logger.success(f"Аккаунт выставлен: https://lzt.market/{item_id}/")
                return True

            errors = result.get("errors") or {}
            if "retry_request" in errors or result.get("message") == "retry_request":
                logger.info(f"goods/check retry_request (попытка {attempt}/100)...")
                continue

            error = (
                result.get("message")
                or result.get("error")
                or json.dumps(result, ensure_ascii=False)
            )
            logger.error(f"Ошибка goods/check (попытка {attempt}): {error}")
            return False

        logger.error("goods/check: исчерпаны все 100 попыток")
        return False

    def sell_account(
        self,
        cookies_json: str,
        tiktok_login: str = "",
        tiktok_password: str = "",
        mail_login: str = "",
        mail_password: str = "",
    ) -> bool:
        if not self.token:
            return False

        description = (
            "Авторег тикток аккаунт Микс IP\n"
            "Есть доступ к почте rambler.ru\n"
            "Выдаются в формате логин:пароль:логин_почты:пароль_почты\n"
            "by y1ki autoreg"
        )

        login_password_str = ""
        if tiktok_login and tiktok_password:
            login_password_str = f"{tiktok_login}:{tiktok_password}"

        data = {
            "price": price,
            "currency": "rub",
            "category_id": 20,
            "item_origin": "autoreg",
            "title": "Авторег тикток аккаунт Микс IP, Rambler почта",
            "description": description,
            "cookies": cookies_json,
        }
        if login_password_str:
            data["login_password"] = login_password_str
        if mail_login and mail_password:
            data["has_email_login_data"] = 1
            data["email_login_data"] = f"{mail_login}:{mail_password}"
            data["email_type"] = "autoreg"

        result = self._post_safe("item/add", data)

        if "item" not in result and result.get("status") != "ok":
            error = (
                result.get("message")
                or result.get("error")
                or json.dumps(result, ensure_ascii=False)
            )
            logger.error(f"Ошибка item/add: {error}")
            return False

        item_id = (result.get("item") or {}).get("item_id", "")
        logger.info(f"Лот создан (item_id={item_id}), запускаем проверку...")
        return self._check_account(item_id, cookies_json, login_password_str)


class TikTokRegistration:
    def __init__(self, config: Config, output_folder: str = "accounts"):
        self.config = config
        self.proxy_manager = ProxyManager(config)
        self.data_generator = DataGenerator()
        self.mail_manager = MailManager(config.mails_filename)
        self.successful_accounts = []
        self.failed_count = 0
        self.accounts_output_folder = output_folder
        self._account_counter = 0
        self._counter_lock = None
        self.lzt_market = LZTMarket(config.lzt_token) if config.lzt_token else None
        os.makedirs(self.accounts_output_folder, exist_ok=True)

    async def run_parallel_registration(self, total: int, windows: int):
        tasks = [self.register_account() for _ in range(total)]
        semaphore = asyncio.Semaphore(windows)

        async def guarded(task):
            async with semaphore:
                return await task

        results = await asyncio.gather(*[guarded(t) for t in tasks], return_exceptions=True)
        successful = sum(1 for r in results if r is True)
        failed = total - successful
        print(f"\nРезультат: успешно={successful}, ошибки={failed}")

    async def register_account(self) -> bool:
        mail_data = await self.mail_manager.get_mail()
        if not mail_data:
            logger.error("Нет доступных почт в mails.txt!")
            return False

        mail_email = mail_data["email"]
        mail_login = mail_data["login"]
        mail_password = mail_data["password"]

        password = self.data_generator.generate_password()
        birth_date = self.data_generator.generate_birth_date()
        user_agent = self.data_generator.get_random_user_agent()

        max_mail_retries = 5
        for mail_attempt in range(max_mail_retries):
            try:
                result = await self._try_register_with_mail(
                    mail_email, mail_login, mail_password,
                    password, birth_date, user_agent
                )
                return result
            except InvalidMailError as e:
                logger.warning(f"[MAIL] {e} — берём новую почту ({mail_attempt + 1}/{max_mail_retries})")
                mail_data = await self.mail_manager.get_mail()
                if not mail_data:
                    logger.error("Нет доступных почт в mails.txt!")
                    return False
                mail_email = mail_data["email"]
                mail_login = mail_data["login"]
                mail_password = mail_data["password"]
                password = self.data_generator.generate_password()
                birth_date = self.data_generator.generate_birth_date()
                continue

        logger.error("Не удалось получить рабочую почту после нескольких попыток")
        return False

    async def _try_register_with_mail(
        self,
        mail_email: str, mail_login: str, mail_password: str,
        password: str, birth_date: dict, user_agent: str,
    ) -> bool:
        try:
            async with async_playwright() as p:
                proxy = self.proxy_manager.get_next_proxy()

                browser_options = {
                    "headless": self.config.browser_headless,
                    "args": self.config.browser_args,
                }
                if proxy:
                    browser_options["proxy"] = proxy
                    logger.info(f"Используем прокси: {proxy['server']}")

                browser = await p.chromium.launch(**browser_options)
                context = await browser.new_context(
                    user_agent=user_agent,
                    viewport={"width": 1920, "height": 1080},
                    locale="ru-RU",
                    timezone_id="Europe/Moscow",
                    ignore_https_errors=True,
                )
                page = await context.new_page()
                page.set_default_timeout(self.config.page_load_timeout * 1000)

                try:
                    try:
                        stealth_config = StealthConfig(
                            navigator_languages=False,
                            navigator_vendor=False,
                            navigator_user_agent=False,
                        )
                        await stealth_async(page, stealth_config)
                    except Exception as e:
                        logger.warning(f"Не удалось применить stealth: {e}")

                    captcha_solver = CaptchaSolver(page, self.config.sadcaptcha_api_key, self.config)

                    logger.info("Переходим на страницу регистрации TikTok")
                    await page.goto(
                        "https://www.tiktok.com/signup/phone-or-email/email",
                        timeout=60000,
                    )
                    await asyncio.sleep(self.config.action_delay)

                    await self._handle_terms_popup(page)

                    success = await self._fill_registration_form(
                        page,
                        mail_email,
                        password,
                        birth_date,
                        captcha_solver,
                        mail_login,
                        mail_password,
                    )

                    if success:
                        self.successful_accounts.append({
                            "email": mail_email,
                            "password": password,
                            "registered_at": datetime.now().isoformat(),
                        })
                        await self._save_cookies_json(
                            page, mail_email, password, mail_login, mail_password
                        )
                        return True
                    else:
                        self.failed_count += 1
                        return False

                except InvalidMailError:
                    raise
                except Exception as e:
                    logger.error(f"Ошибка при регистрации {mail_email}: {type(e).__name__}: {str(e)}")
                    self.failed_count += 1
                    return False
                finally:
                    await browser.close()

        except InvalidMailError:
            raise
        except Exception as e:
            logger.error(f"Критическая ошибка: {type(e).__name__}: {str(e)}")
            self.failed_count += 1
            return False

    async def _handle_terms_popup(self, page: Page):
        try:
            await asyncio.sleep(0.8)
            accept_buttons = [
                'button:has-text("Принять")',
                'button:has-text("Accept")',
                'button:has-text("Agree")',
                'button:has-text("Согласиться")',
                'button:has-text("OK")',
                'button:has-text("Continue")',
                '[data-e2e="accept-button"]',
                '[data-testid="accept-button"]',
            ]
            for selector in accept_buttons:
                try:
                    button = await page.query_selector(selector)
                    if button and await button.is_visible():
                        await button.click()
                        await asyncio.sleep(0.3)
                        return True
                except:
                    continue
            return False
        except Exception:
            return False

    async def _fill_registration_form(
        self,
        page: Page,
        email: str,
        password: str,
        birth_date: Dict,
        captcha_solver,
        mail_login: str = "",
        mail_password: str = "",
    ) -> bool:
        try:
            month_filled = day_filled = year_filled = False
            date_filled = False

            await asyncio.sleep(2)
            await self._handle_terms_popup(page)

            all_selects = await page.query_selector_all("select")

            
            if len(all_selects) >= 3:
                try:
                    month_select = all_selects[0]
                    month_value = birth_date["month"]
                    month_formats = [
                        month_value,
                        str(int(month_value)).zfill(2),
                        str(int(month_value) - 1),
                        str(int(month_value) - 1).zfill(2),
                    ]
                    month_filled = False
                    for fmt in month_formats:
                        try:
                            await month_select.select_option(value=fmt)
                            month_filled = True
                            break
                        except:
                            continue
                    if not month_filled:
                        await month_select.click()
                        await asyncio.sleep(0.2)
                        month_option = await month_select.query_selector(
                            f"option:nth-child({int(month_value) + 1})"
                        )
                        if month_option:
                            await month_option.click()
                            month_filled = True

                    await asyncio.sleep(0.3)

                    day_select = all_selects[1]
                    day_value = birth_date["day"]
                    day_formats = [
                        day_value,
                        str(int(day_value)).zfill(2),
                        str(int(day_value) - 1),
                        str(int(day_value) - 1).zfill(2),
                    ]
                    day_filled = False
                    for fmt in day_formats:
                        try:
                            await day_select.select_option(value=fmt)
                            day_filled = True
                            break
                        except:
                            continue
                    if not day_filled:
                        await day_select.click()
                        await asyncio.sleep(0.2)
                        day_option = await day_select.query_selector(
                            f"option:nth-child({int(day_value) + 1})"
                        )
                        if day_option:
                            await day_option.click()
                            day_filled = True

                    await asyncio.sleep(0.3)

                    year_select = all_selects[2]
                    year_value = birth_date["year"]
                    year_filled = False
                    for fmt in [year_value, str(year_value)]:
                        try:
                            await year_select.select_option(value=fmt)
                            year_filled = True
                            break
                        except:
                            continue
                    if not year_filled:
                        year_options = await year_select.query_selector_all("option")
                        for option in year_options:
                            try:
                                option_text = await option.inner_text()
                                if year_value in option_text:
                                    await option.click()
                                    year_filled = True
                                    break
                            except:
                                continue

                    if month_filled and day_filled and year_filled:
                        date_filled = True

                except Exception as e:
                    logger.error(f"Ошибка заполнения select дат: {e}")

            
            if not date_filled:
                try:
                    month_names = {
                        1: ["январь"], 2: ["февраль"], 3: ["март"],
                        4: ["апрель"], 5: ["май"], 6: ["июнь"],
                        7: ["июль"], 8: ["август"], 9: ["сентябрь"],
                        10: ["октябрь"], 11: ["ноябрь"], 12: ["декабрь"],
                    }
                    month_num = int(birth_date["month"])
                    month_options = month_names.get(month_num, [birth_date["month"]])
                    day_options = [birth_date["day"], str(int(birth_date["day"]))]
                    year_options = [birth_date["year"]]

                    comboboxes = []
                    month_locators = []
                    day_locators = []
                    year_locators = []

                    try:
                        comboboxes = await page.get_by_role("combobox").all()
                        for cb in comboboxes:
                            try:
                                accessible_name = await cb.get_attribute("aria-label") or ""
                                inner_text = await cb.inner_text()
                                combined = accessible_name.lower() + inner_text.lower()
                                if any(w in combined for w in ["месяц", "month"]):
                                    month_locators.append(cb)
                                elif any(w in combined for w in ["день", "day"]):
                                    day_locators.append(cb)
                                elif any(w in combined for w in ["год", "year"]):
                                    year_locators.append(cb)
                            except:
                                continue
                    except:
                        pass

                    if not day_locators:
                        try:
                            selects = await page.query_selector_all("select")
                            for s in selects:
                                day_locators.append(s)
                        except:
                            pass

                    month_filled = False
                    if month_locators:
                        try:
                            month_cb = month_locators[0]
                            await month_cb.scroll_into_view_if_needed()
                            await month_cb.click()
                            await asyncio.sleep(1)
                            try:
                                await page.wait_for_selector('[role="listbox"]:visible', timeout=3000)
                            except:
                                pass

                            for month_text in month_options:
                                try:
                                    option = page.get_by_role("option", name=month_text, exact=True)
                                    if await option.count() > 0:
                                        await option.first.click()
                                        month_filled = True
                                        break
                                except:
                                    pass
                                try:
                                    options = await page.query_selector_all('[role="option"]:visible')
                                    for opt in options:
                                        opt_text = await opt.inner_text()
                                        if month_text.lower() in opt_text.lower():
                                            await opt.click()
                                            month_filled = True
                                            break
                                    if month_filled:
                                        break
                                except:
                                    pass
                        except Exception as e:
                            logger.error(f"Ошибка заполнения месяца: {e}")

                    await asyncio.sleep(0.3)

                    day_filled = False
                    if day_locators:
                        try:
                            day_elem = day_locators[0]
                            if hasattr(day_elem, "tag_name"):
                                tag_name = await day_elem.tag_name()
                                if tag_name == "select":
                                    await day_elem.select_option(birth_date["day"])
                                    day_filled = True
                                else:
                                    await day_elem.click()
                                    await asyncio.sleep(0.8)
                                    for day_text in day_options:
                                        try:
                                            option = page.get_by_role("option", name=day_text, exact=True)
                                            if await option.count() > 0:
                                                await option.first.click()
                                                day_filled = True
                                                break
                                        except:
                                            continue
                            else:
                                await day_elem.scroll_into_view_if_needed()
                                await day_elem.click()
                                await asyncio.sleep(0.8)
                                try:
                                    await page.wait_for_selector('[role="listbox"]:visible', timeout=3000)
                                except:
                                    pass
                                for day_text in day_options:
                                    try:
                                        option = page.get_by_role("option", name=day_text, exact=True)
                                        if await option.count() > 0:
                                            await option.first.click()
                                            day_filled = True
                                            break
                                    except:
                                        continue
                        except Exception as e:
                            logger.error(f"Ошибка заполнения дня: {e}")

                    await asyncio.sleep(0.3)

                    year_filled = False
                    if year_locators:
                        try:
                            year_cb = year_locators[0]
                            await year_cb.scroll_into_view_if_needed()
                            await year_cb.click()
                            await asyncio.sleep(0.8)
                            try:
                                await page.wait_for_selector('[role="listbox"]:visible', timeout=3000)
                            except:
                                pass
                            for year_text in year_options:
                                try:
                                    option = page.get_by_role("option", name=year_text, exact=True)
                                    if await option.count() > 0:
                                        await option.first.click()
                                        year_filled = True
                                        break
                                except:
                                    continue
                                try:
                                    options = await page.query_selector_all('[role="option"]:visible')
                                    for opt in options:
                                        opt_text = await opt.inner_text()
                                        if year_text in opt_text:
                                            await opt.click()
                                            year_filled = True
                                            break
                                    if year_filled:
                                        break
                                except:
                                    pass
                        except Exception as e:
                            logger.error(f"Ошибка заполнения года: {e}")

                    if month_filled or day_filled or year_filled:
                        date_filled = True

                except Exception as e:
                    logger.error(f"Ошибка заполнения даты (способ 2): {e}")

            await asyncio.sleep(0.3)

            try:
                next_buttons = await page.query_selector_all("button")
                for btn in next_buttons:
                    try:
                        btn_text = await btn.inner_text()
                        is_enabled = await btn.is_enabled()
                        if (
                            any(w in btn_text.lower() for w in ["далее", "next", "продолжить", "continue"])
                            and is_enabled
                        ):
                            await btn.click()
                            await asyncio.sleep(1)
                            break
                    except:
                        continue
            except Exception as e:
                logger.error(f"Ошибка нажатия 'Далее' после даты: {e}")

            await asyncio.sleep(1)
            await self._handle_terms_popup(page)
            await asyncio.sleep(1)

            email_selectors = [
                'input[type="email"]',
                'input[name="email"]',
                'input[placeholder*="email"]',
                'input[placeholder*="почт"]',
                'input[placeholder*="Email"]',
            ]
            email_input = None
            for selector in email_selectors:
                try:
                    email_input = await page.query_selector(selector)
                    if email_input and await email_input.is_visible():
                        break
                    email_input = None
                except:
                    continue

            if email_input:
                await email_input.fill(email)
            else:
                logger.warning("Поле email не найдено")

            await asyncio.sleep(0.3)

            password_inputs = await page.query_selector_all('input[type="password"]')
            if password_inputs:
                await password_inputs[0].fill(password)
            else:
                logger.warning("Поле пароля не найдено")

            await asyncio.sleep(0.5)

            try:
                all_inputs = await page.query_selector_all("input")
                for input_elem in all_inputs:
                    try:
                        input_type = await input_elem.get_attribute("type")
                        is_visible = await input_elem.is_visible()
                        if input_type == "checkbox" and is_visible:
                            is_checked_before = await input_elem.is_checked()
                            for _ in range(3):
                                box = await input_elem.bounding_box()
                                if box:
                                    center_x = box["x"] + box["width"] / 2
                                    center_y = box["y"] + box["height"] / 2
                                    await page.mouse.click(center_x, center_y)
                                else:
                                    await input_elem.click()
                                await asyncio.sleep(0.2)
                                if await input_elem.is_checked() != is_checked_before:
                                    break
                            break
                    except:
                        continue
            except Exception as e:
                logger.error(f"Ошибка поиска чекбокса: {e}")

            await asyncio.sleep(0.5)

            send_code_clicked = False
            all_buttons = await page.query_selector_all("button")
            for button_elem in all_buttons:
                try:
                    button_text = await button_elem.inner_text()
                    is_enabled = await button_elem.is_enabled()
                    button_disabled = await button_elem.get_attribute("disabled")
                    if (
                        button_text
                        and any(kw in button_text.lower() for kw in ["отправить код", "send code", "отправить"])
                        and is_enabled
                        and not button_disabled
                    ):
                        await button_elem.click()
                        send_code_clicked = True
                        break
                except:
                    continue

            if not send_code_clicked:
                logger.error("Кнопка 'Отправить код' не найдена")

            await asyncio.sleep(1)

            try:
                code_input = await page.wait_for_selector(
                    'input[placeholder*="код"], input[placeholder*="code"], input[maxlength="6"]',
                    timeout=10000,
                )
                if code_input:
                    verification_code = await self.mail_manager.get_verification_code(
                        mail_login, mail_password
                    )

                    if verification_code:
                        logger.success(f"Код получен: {verification_code}")
                        await code_input.fill("")
                        await code_input.type(verification_code, delay=30)

                        final_buttons = await page.query_selector_all("button")
                        for button in final_buttons:
                            try:
                                btn_text = await button.inner_text()
                                is_enabled = await button.is_enabled()
                                if (
                                    any(w in btn_text.lower() for w in ["далее", "next", "продолжить", "continue"])
                                    and is_enabled
                                ):
                                    await button.click()
                                    break
                            except:
                                continue
                    else:
                        logger.warning(f"Не удалось получить код. Введите вручную для: {email}")
                        await asyncio.sleep(30)

            except InvalidMailError:
                raise
            except Exception as e:
                logger.error(f"Ошибка ожидания поля кода: {e}")

            await asyncio.sleep(1)

            all_buttons = await page.query_selector_all("button")
            for button_elem in all_buttons:
                try:
                    button_text = await button_elem.inner_text()
                    button_disabled = await button_elem.get_attribute("disabled")
                    if (
                        button_text
                        and any(kw in button_text.lower() for kw in ["далее", "next", "регистр", "sign up", "продолжить", "continue"])
                        and not button_disabled
                    ):
                        await button_elem.click()
                        break
                except:
                    continue

            await asyncio.sleep(1)
            await captcha_solver.solve_captcha_if_present()
            await asyncio.sleep(2)

            try:
                await asyncio.sleep(2)
                current_url = page.url

                if (
                    "following" in current_url
                    or "foryou" in current_url
                    or "welcome" in current_url
                    or "onboarding" in current_url
                    or "verification" in current_url
                    or "signup" not in current_url
                ):
                    logger.success("Регистрация завершена успешно!")
                    return True

                if "create-username" in current_url:
                    skip_clicked = await _click_skip_button(page)
                    if not skip_clicked:
                        logger.warning("Кнопка 'Пропустить' не найдена на странице create-username")
                    await asyncio.sleep(1)
                    return True

                code_inputs = await page.query_selector_all(
                    'input[placeholder*="код"], input[placeholder*="code"], input[maxlength="6"]'
                )
                if code_inputs:
                    logger.success("Дошли до этапа подтверждения email!")
                    return True

                logger.warning(f"Неопределенное состояние: {current_url}")
                return False

            except InvalidMailError:
                raise
            except Exception as e:
                logger.error(f"Ошибка проверки результата: {e}")
                return False

        except InvalidMailError:
            raise
        except Exception as e:
            logger.error(f"Ошибка заполнения формы: {type(e).__name__}: {str(e)}")
            return False

    def _save_account_to_file(self, login_password: str):
        """Сохраняет аккаунт в acc.txt в формате логин:пароль:логин_почты:пароль_почты"""
        try:
            filename = self.config.accounts_filename
            with open(filename, "a", encoding="utf-8") as f:
                f.write(login_password + "\n")
            print(f"[ACC] Аккаунт сохранён в {filename}: {login_password}")
        except Exception as e:
            logger.error(f"Ошибка записи в acc.txt: {e}")

    async def _save_cookies_json(
        self,
        page,
        tiktok_email: str = "",
        tiktok_password: str = "",
        mail_login: str = "",
        mail_password: str = "",
    ) -> str:
        if self._counter_lock is None:
            self._counter_lock = asyncio.Lock()

        async with self._counter_lock:
            self._account_counter += 1
            file_number = self._account_counter

        try:
            os.makedirs(self.accounts_output_folder, exist_ok=True)
            filepath = os.path.join(self.accounts_output_folder, f"{file_number}.json")

            raw_cookies = await page.context.cookies()

            KEY_COOKIES = {"sid_guard", "tt-target-idc", "msToken"}

            formatted = []
            cookie_id = 0
            for c in raw_cookies:
                name = c.get("name", "")
                if name not in KEY_COOKIES:
                    continue
                expires = c.get("expires", -1)
                if expires is None or expires <= 0:
                    expires = 2147483647
                formatted.append({
                    "domain": c.get("domain", ""),
                    "expirationDate": int(expires),
                    "expiration": int(expires),
                    "hostOnly": c.get("httpOnly", False),
                    "httpOnly": c.get("httpOnly", False),
                    "name": name,
                    "path": c.get("path", "/"),
                    "sameSite": "unspecified",
                    "secure": c.get("secure", True),
                    "session": False,
                    "storeId": "0",
                    "value": c.get("value", ""),
                    "id": cookie_id,
                })
                cookie_id += 1

            lines = []
            for cookie in formatted:
                line = json.dumps(cookie, ensure_ascii=False, separators=(",", ":"))
                line = line.replace("/", "\\/")
                lines.append("  " + line)
            output = "[\n" + ",\n".join(lines) + "\n]"

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(output)

            logger.success(f"Куки сохранены: {filepath} ({len(formatted)} cookies)")

            login_password_txt = ""
            if tiktok_email and tiktok_password:
                login_password_txt = f"{tiktok_email}:{tiktok_password}"

            if self.lzt_market:
                sold = await asyncio.get_event_loop().run_in_executor(
                    None, self.lzt_market.sell_account,
                    output, tiktok_email, tiktok_password, mail_login, mail_password
                )
                if not sold and login_password_txt:
                    self._save_account_to_file(login_password_txt)
            elif login_password_txt:
                self._save_account_to_file(login_password_txt)

            return filepath

        except Exception as e:
            logger.error(f"Ошибка сохранения куков: {e}")
            return ""


def main():
    config = load_config()

    print("=" * 60)
    print("  TikTok Auto Registration")
    print("=" * 60)

    if config.lzt_token:
        lzt = LZTMarket(config.lzt_token)
        lzt.check_token()
    else:
        print("LZT токен не указан — куки сохраняются в accounts/")

    if not os.path.exists(config.mails_filename):
        print(f"Файл {config.mails_filename} не найден!")
        print("Создайте файл с почтами в формате: login;password")
        return

    if not os.path.exists(config.proxies_filename):
        print(f"Файл {config.proxies_filename} не найден!")
        print("Создайте файл с прокси в формате: ip:port:username:password")
        return

    print("Нажмите Ctrl+C для остановки\n")

    cycle = 1
    try:
        while True:
            print(f"\n{'=' * 60}")
            print(f"  ЦИКЛ #{cycle} — регистрируем {count} аккаунтов в {count_window} окнах")
            print(f"{'=' * 60}")

            registrator = TikTokRegistration(config, "accounts")
            asyncio.run(registrator.run_parallel_registration(count, count_window))

            print(f"\nЦикл #{cycle} завершён.")
            print(f"Перерыв {break_time} минут перед следующим циклом...")

            for remaining in range(break_time * 60, 0, -30):
                mins = remaining // 60
                secs = remaining % 60
                print(f"   Осталось: {mins:02d}:{secs:02d}", end="\r")
                time.sleep(30)

            print()
            cycle += 1

    except KeyboardInterrupt:
        print("\nРегистрация остановлена пользователем")
    except Exception as e:
        print(f"\nКритическая ошибка: {e}")


if __name__ == "__main__":
    main()
