import os
import uuid
import re
import json
import random  # For picking a random name
from datetime import datetime, timedelta

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from dateutil import parser
import logging
import html

# Import email_validator for RFC/DNS email validation
from email_validator import validate_email as validate_email_func, EmailNotValidError
from difflib import SequenceMatcher  # For fuzzy matching of domains

# Managers
from openai_manager import OpenAIManager
from maps_manager import MapsManager
from faq_manager import FAQManager

##############################################
# GLOBALS FOR BOT NAMES (in‑memory, not stored in DB)
##############################################
BOT_NAMES_LIST = ["Alice", "Bob", "Charlie", "David", "Emma", "Fiona", "George", "Hannah", "Ivan", "Julia"]
# This dictionary will map chat_id (string) to the randomly chosen bot name
BOT_NAMES_MAP = {}

##############################################
# FLASK APP SETUP + CORS + SQLALCHEMY CONFIG
##############################################
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(os.getcwd(), "chatbot.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

##############################################
# DATABASE MODELS
##############################################

class ChatState:
    INITIAL = "INITIAL"
    COLLECTING_MOVE_SIZE = "COLLECTING_MOVE_SIZE"
    COLLECTING_MOVE_DATE = "COLLECTING_MOVE_DATE"
    COST_ESTIMATED = "COST_ESTIMATED"
    # Old state if you want to keep it for backward compatibility:
    AWAITING_DETAILS = "AWAITING_DETAILS"  
    # New states for separate prompts:
    AWAITING_NAME = "AWAITING_NAME"
    AWAITING_CONTACT = "AWAITING_CONTACT"
    AWAITING_FINAL_CONFIRMATION = "AWAITING_FINAL_CONFIRMATION"
    MODIFY_DETAILS = "MODIFY_DETAILS"
    CONFIRMED = "CONFIRMED"

class ChatSession(db.Model):
    __tablename__ = "chat_sessions"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    chat_id = db.Column(db.String(100), unique=True, nullable=False)
    username = db.Column(db.String(100), nullable=True)
    contact_no = db.Column(db.String(50), nullable=True)
    move_date = db.Column(db.String(100), nullable=True)
    estimated_cost_min = db.Column(db.Float, nullable=True)
    estimated_cost_max = db.Column(db.Float, nullable=True)
    confirmed = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    state = db.Column(db.String(50), default=ChatState.INITIAL)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    messages = db.relationship("Message", backref="chat_session", lazy=True)
    move_detail = db.relationship("MoveDetail", backref="chat_session", uselist=False)

    def __repr__(self):
        return f"<ChatSession {self.chat_id}>"

class Message(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    chat_id = db.Column(db.String(100), db.ForeignKey("chat_sessions.chat_id"), nullable=False)
    sender = db.Column(db.String(20), nullable=False)  # 'user' or 'assistant'
    message = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Message {self.id} from {self.sender}>"

class MoveDetail(db.Model):
    __tablename__ = "move_details"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    chat_id = db.Column(db.String(100), db.ForeignKey("chat_sessions.chat_id"), unique=True, nullable=False)
    origin = db.Column(db.String(100), nullable=True)
    destination = db.Column(db.String(100), nullable=True)
    move_size = db.Column(db.String(100), nullable=True)
    additional_services = db.Column(db.String(200), nullable=True)
    move_date = db.Column(db.String(100), nullable=True)
    username = db.Column(db.String(100), nullable=True)
    contact_no = db.Column(db.String(50), nullable=True)
    email = db.Column(db.String(120), nullable=True)
    estimated_cost_min = db.Column(db.Float, nullable=True)
    estimated_cost_max = db.Column(db.Float, nullable=True)
    state = db.Column(db.String(50), default=ChatState.INITIAL)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<MoveDetail {self.chat_id}>"

with app.app_context():
    db.create_all()

##############################################
# INIT MANAGERS
##############################################
openai_manager = OpenAIManager()
maps_manager = MapsManager()
faq_manager = FAQManager()
faq_manager.load_faqs("/Users/TalhaZain/chatbot_html/backend/data/faqs.jsonl")

##############################################
# LOGGER CONFIGURATION
##############################################
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

##############################################
# HELPER FUNCTIONS
##############################################

def create_short_system_prompt(bot_name="MoveBot"):
    return (
        f"You are {bot_name} 🤖, a friendly assistant for My Good Movers. "
        "My Good Movers is a platform that connects users and moving companies. "
        "Try to convince the user to take our services, and provide them with the estimated cost of their move. "
        "Use emoticons to make your responses more friendly and engaging. "
        "Keep your answers brief, no more than 2 short sentences."
    )

def is_faq_query(user_text):
    keywords = [
        "modify booking",
        "hidden charge",
        "refund",
        "cancel",
        "policy",
        "charges",
        "payment",
        "change booking"
    ]
    return any(kw in user_text.lower() for kw in keywords)

def standardize_date(date_str):
    try:
        parsed_date = parser.parse(date_str, fuzzy=True)
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if parsed_date < today:
            return None, "The date you provided is in the past. Please provide a future date."
        return parsed_date.strftime("%Y-%m-%d"), None
    except Exception as e:
        logger.error(f"Date Parsing Error: {e}")
        return None, "Invalid date format. Please provide valid date."

def parse_move_details_with_openai(user_text):
    system_prompt = (
        "You are a JSON parser for a moving service chatbot. The user may provide details about their move.\n"
        "Extract the following information into JSON with these exact keys: origin, destination, move_size, move_date, additional_services, username, contact_no.\n"
        "If a field is not mentioned, set it to null or an empty array.\n"
        "Return only JSON, no extra text."
    )
    extraction_response = openai_manager.extract_fields_from_text(system_prompt, user_text)
    logger.debug(f"OpenAI Extraction Response: {extraction_response}")
    data = extraction_response if isinstance(extraction_response, dict) else {}
    return {
        "origin": data.get("origin"),
        "destination": data.get("destination"),
        "move_size": data.get("move_size"),
        "move_date": data.get("move_date"),
        "additional_services": data.get("additional_services") or [],
        "username": data.get("username"),
        "contact_no": data.get("contact_no")
    }

def normal_gpt_reply(chat_session, user_text):
    # Retrieve bot name from our in-memory mapping (default to "MoveBot" if not found)
    bot_name = BOT_NAMES_MAP.get(chat_session.chat_id, "MoveBot")
    system_prompt = create_short_system_prompt(bot_name)
    combined_input = f"Chat History:\n{get_chat_history(chat_session.chat_id)}\nUser: {user_text}"
    gpt_response = openai_manager.get_general_response(
        system_content=system_prompt,
        user_content=combined_input
    )
    assistant_message = Message(
        chat_id=chat_session.chat_id,
        sender="assistant",
        message=gpt_response
    )
    db.session.add(assistant_message)
    db.session.commit()
    return gpt_response

def get_chat_history(chat_id):
    messages = Message.query.filter_by(chat_id=chat_id).order_by(Message.timestamp).all()
    return "\n".join([f"{msg.sender.capitalize()}: {msg.message}" for msg in messages])

def sanitize_input(user_input):
    return html.escape(user_input)

def validate_and_normalize_contact_number(contact):
    digits = re.sub(r'\D', '', contact)
    if len(digits) == 10:
        return digits
    return None

def validate_email(email):
    try:
        valid = validate_email_func(email, check_deliverability=True)
        normalized_email = valid["email"]
        local, domain = normalized_email.split("@")
        domain_name = domain.split(".")[0]
        if local.lower() == domain_name.lower():
            raise EmailNotValidError("Local part and domain part cannot be identical.")
        common_domains = ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com"]
        for common in common_domains:
            ratio = SequenceMatcher(None, domain.lower(), common).ratio()
            if ratio > 0.85 and domain.lower() != common:
                raise EmailNotValidError(f"Email domain seems invalid. Did you mean {common}?")
        return normalized_email
    except EmailNotValidError as e:
        return None

def collect_or_update_move_details(chat_session, user_text):
    extracted = parse_move_details_with_openai(user_text)
    provided_fields = [extracted.get("origin"), extracted.get("destination"),
                       extracted.get("move_size"), extracted.get("move_date")]
    any_new_info = any(provided_fields)
    if not any_new_info:
        return (False, False, None)
    move_detail = chat_session.move_detail
    if not move_detail:
        move_detail = MoveDetail(chat_id=chat_session.chat_id)
        db.session.add(move_detail)
        db.session.commit()
    if extracted.get("origin"):
        move_detail.origin = extracted["origin"]
    if extracted.get("destination"):
        move_detail.destination = extracted["destination"]
    if extracted.get("move_size"):
        move_detail.move_size = extracted["move_size"]
    if extracted.get("additional_services"):
        move_detail.additional_services = ",".join(extracted["additional_services"])
    if extracted.get("username"):
        move_detail.username = extracted["username"]
    if extracted.get("contact_no"):
        move_detail.contact_no = extracted["contact_no"]
    if extracted.get("move_date"):
        std_date, err = standardize_date(extracted["move_date"])
        if err:
            db.session.commit()
            return (True, False, f"{err} Please provide a valid future date.")
        move_detail.move_date = std_date
        chat_session.move_date = std_date
    db.session.commit()
    missing = []
    if not move_detail.origin:
        missing.append("origin")
    if not move_detail.destination:
        missing.append("destination")
    if not move_detail.move_size:
        missing.append("move size")
    if not move_detail.move_date:
        missing.append("move date")
    if missing:
        fields_str = ", ".join(missing)
        reply = f"I still need your {fields_str} to provide an estimate."
        return (True, False, reply)
    distance, cost_range = maps_manager.estimate_cost(
        move_detail.origin,
        move_detail.destination,
        move_detail.move_size,
        move_detail.additional_services.split(',') if move_detail.additional_services else [],
        move_detail.move_date
    )
    if distance is None or not isinstance(cost_range, tuple):
        reply = "I'm having trouble calculating the cost. Please verify locations or try again."
        return (True, False, reply)
    min_cost, max_cost = cost_range
    chat_session.estimated_cost_min = min_cost
    chat_session.estimated_cost_max = max_cost
    move_detail.estimated_cost_min = min_cost
    move_detail.estimated_cost_max = max_cost
    chat_session.state = ChatState.COST_ESTIMATED
    move_detail.state = ChatState.COST_ESTIMATED
    db.session.commit()
    estimate_reply = (
        f"The estimated cost for moving from {move_detail.origin.title()} to {move_detail.destination.title()} "
        f"({move_detail.move_size.title()}, date: {move_detail.move_date}) is between ${min_cost} and ${max_cost}. 🏠📦💰\n"
        f"Would you like to proceed with booking this move? (Reply Yes/No) 👍👎"
    )
    return (True, True, estimate_reply)

##############################################
# FLASK ROUTES
##############################################

@app.route("/", methods=["GET"])
def home():
    return render_template('index.html')

@app.route("/start_chat", methods=["POST"])
def start_chat():
    try:
        chat_id = str(uuid.uuid4())
        logger.info(f"Starting new chat session with chat_id={chat_id}")
        chat_session = ChatSession(chat_id=chat_id, state=ChatState.INITIAL)
        # Pick a random name from our list and store it in the in-memory mapping.
        chosen_bot_name = random.choice(BOT_NAMES_LIST)
        BOT_NAMES_MAP[chat_id] = chosen_bot_name
        db.session.add(chat_session)
        db.session.commit()
        welcome_msg = f"Hello! I'm {chosen_bot_name} 🤖. How can I assist you with your move today? 📦🚚"
        welcome = Message(chat_id=chat_id, sender="assistant", message=welcome_msg)
        db.session.add(welcome)
        db.session.commit()
        return jsonify({"chat_id": chat_id, "message": welcome_msg}), 200
    except Exception as e:
        logger.error(f"Error in /start_chat: {e}")
        return jsonify({"error": "Unable to start chat."}), 500

@app.route("/end_chat", methods=["POST"])
def end_chat():
    try:
        data = request.get_json() or {}
        chat_id = data.get("chat_id")
        if not chat_id:
            return jsonify({"error": "No chat_id provided"}), 400
        chat_session = ChatSession.query.filter_by(chat_id=chat_id).first()
        if not chat_session:
            return jsonify({"error": "Chat session not found"}), 404
        farewell_msg = "Chat ended successfully. Thank you for choosing My Good Movers! 👋"
        msg = Message(chat_id=chat_id, sender="assistant", message=farewell_msg)
        db.session.add(msg)
        chat_session.is_active = False
        db.session.add(chat_session)
        db.session.commit()
        return jsonify({"message": farewell_msg}), 200
    except Exception as e:
        logger.error(f"Error in /end_chat: {e}")
        return jsonify({"error": "Unable to end chat."}), 500

@app.route("/general_query", methods=["POST"])
def general_query():
    try:
        data = request.get_json() or {}
        user_input = data.get("message", "").strip()
        chat_id = data.get("chat_id")
        if not chat_id:
            return jsonify({"error": "Missing chat_id"}), 400
        chat_session = ChatSession.query.filter_by(chat_id=chat_id).first()
        if not chat_session:
            return jsonify({"error": "Chat session not found"}), 404
        if not chat_session.is_active and chat_session.state != ChatState.CONFIRMED:
            return jsonify({"error": "Chat session is already ended. Please start a new chat."}), 400
        user_input = sanitize_input(user_input)
        user_msg = Message(chat_id=chat_id, sender="user", message=user_input)
        db.session.add(user_msg)
        db.session.commit()
        if is_faq_query(user_input):
            answer = faq_manager.find_best_match(user_input)
            bot_msg = Message(chat_id=chat_id, sender="assistant", message=answer)
            db.session.add(bot_msg)
            db.session.commit()
            return jsonify({"reply": answer, "chat_id": chat_id}), 200
        collecting_states = [
            ChatState.INITIAL,
            ChatState.COLLECTING_MOVE_SIZE,
            ChatState.COLLECTING_MOVE_DATE,
            ChatState.MODIFY_DETAILS
        ]
        if chat_session.state in collecting_states:
            any_new_info, did_estimate, reply_message = collect_or_update_move_details(chat_session, user_input)
            if any_new_info:
                if reply_message:
                    bot_msg = Message(chat_id=chat_id, sender="assistant", message=reply_message)
                    db.session.add(bot_msg)
                    db.session.commit()
                    return jsonify({"reply": reply_message, "chat_id": chat_id}), 200
        if chat_session.state == ChatState.COST_ESTIMATED:
            if user_input.lower() in ["yes", "y", "👍"]:
                chat_session.state = ChatState.COLLECTING_MOVE_SIZE
                move_detail = chat_session.move_detail
                if move_detail and move_detail.move_size:
                    additional_costs = maps_manager.get_additional_services_costs(move_detail.move_size)
                    reply = (
                        f"Would you like any additional services such as packing (cost: ${additional_costs.get('packing')}) "
                        f"or storage (cost: ${additional_costs.get('storage')})? If yes, please specify them "
                        f"(e.g., 'only packing', 'yes storage', or 'packing, storage'). If not, type 'no'."
                    )
                else:
                    reply = (
                        "Would you like any additional services such as packing or storage? "
                        "If yes, please specify them (e.g., packing, storage). If not, type 'no'."
                    )
                bot_msg = Message(chat_id=chat_id, sender="assistant", message=reply)
                db.session.add(bot_msg)
                db.session.commit()
                return jsonify({"reply": reply, "chat_id": chat_id}), 200
            elif user_input.lower() in ["no", "n", "👎"]:
                chat_session.state = ChatState.INITIAL
                db.session.commit()
                reply = "No worries! Let me know if you have any other questions."
                bot_msg = Message(chat_id=chat_id, sender="assistant", message=reply)
                db.session.add(bot_msg)
                db.session.commit()
                return jsonify({"reply": reply, "chat_id": chat_id}), 200
            else:
                reply = "Please respond with Yes or No. Would you like to proceed with booking?"
                bot_msg = Message(chat_id=chat_id, sender="assistant", message=reply)
                db.session.add(bot_msg)
                db.session.commit()
                return jsonify({"reply": reply, "chat_id": chat_id}), 200
        if chat_session.state == ChatState.COLLECTING_MOVE_SIZE:
            move_detail = chat_session.move_detail
            if not move_detail:
                fallback_reply = "No move details found. Please provide origin, destination, move size, and move date first."
                bot_msg = Message(chat_id=chat_id, sender="assistant", message=fallback_reply)
                db.session.add(bot_msg)
                db.session.commit()
                return jsonify({"reply": fallback_reply, "chat_id": chat_id}), 200
            user_lower = user_input.lower().strip()
            if user_lower in ["no", "none"]:
                move_detail.additional_services = ""
            else:
                services_found = []
                if "packing" in user_lower:
                    services_found.append("packing")
                if "storage" in user_lower:
                    services_found.append("storage")
                if not services_found:
                    reply = "Sorry, please respond again with valid additional services (e.g., packing, storage) or 'no'."
                    bot_msg = Message(chat_id=chat_id, sender="assistant", message=reply)
                    db.session.add(bot_msg)
                    db.session.commit()
                    return jsonify({"reply": reply, "chat_id": chat_id}), 200
                move_detail.additional_services = ",".join(services_found)
            db.session.commit()
            distance, cost_range = maps_manager.estimate_cost(
                move_detail.origin,
                move_detail.destination,
                move_detail.move_size,
                move_detail.additional_services.split(',') if move_detail.additional_services else [],
                move_detail.move_date
            )
            if distance is not None and isinstance(cost_range, tuple):
                min_cost, max_cost = cost_range
                chat_session.estimated_cost_min = min_cost
                chat_session.estimated_cost_max = max_cost
                move_detail.estimated_cost_min = min_cost
                move_detail.estimated_cost_max = max_cost
                db.session.commit()
            chat_session.state = ChatState.COLLECTING_MOVE_DATE
            db.session.commit()
            reply = "Please share your email address for updates on your move."
            bot_msg = Message(chat_id=chat_id, sender="assistant", message=reply)
            db.session.add(bot_msg)
            db.session.commit()
            return jsonify({"reply": reply, "chat_id": chat_id}), 200
        elif chat_session.state == ChatState.COLLECTING_MOVE_DATE:
            move_detail = chat_session.move_detail
            if not move_detail:
                fallback_reply = "No move details found. Please provide origin, destination, move size, and move date first."
                bot_msg = Message(chat_id=chat_id, sender="assistant", message=fallback_reply)
                db.session.add(bot_msg)
                db.session.commit()
                return jsonify({"reply": fallback_reply, "chat_id": chat_id}), 200
            email = user_input.strip()
            normalized_email = validate_email(email)
            if not normalized_email:
                reply = "Invalid email format. Please provide a valid email address."
                bot_msg = Message(chat_id=chat_id, sender="assistant", message=reply)
                db.session.add(bot_msg)
                db.session.commit()
                return jsonify({"reply": reply, "chat_id": chat_id}), 200
            move_detail.email = normalized_email
            db.session.commit()
            chat_session.state = ChatState.AWAITING_NAME
            db.session.commit()
            next_prompt = "Great! Now please share your name."
            bot_msg = Message(chat_id=chat_id, sender="assistant", message=next_prompt)
            db.session.add(bot_msg)
            db.session.commit()
            return jsonify({"reply": next_prompt, "chat_id": chat_id}), 200
        elif chat_session.state == ChatState.AWAITING_NAME:
            name = user_input.strip()
            if not name:
                reply = "Please provide your name."
                bot_msg = Message(chat_id=chat_id, sender="assistant", message=reply)
                db.session.add(bot_msg)
                db.session.commit()
                return jsonify({"reply": reply, "chat_id": chat_id}), 200
            chat_session.username = name
            move_detail = chat_session.move_detail
            if move_detail:
                move_detail.username = name
            chat_session.state = ChatState.AWAITING_CONTACT
            db.session.commit()
            next_prompt = "Thank you! Now please share your 10-digit contact number."
            bot_msg = Message(chat_id=chat_id, sender="assistant", message=next_prompt)
            db.session.add(bot_msg)
            db.session.commit()
            return jsonify({"reply": next_prompt, "chat_id": chat_id}), 200
        elif chat_session.state == ChatState.AWAITING_CONTACT:
            contact = user_input.strip()
            normalized_contact = validate_and_normalize_contact_number(contact)
            if not normalized_contact:
                reply = "Invalid contact number format. Please provide a valid 10-digit contact number."
                bot_msg = Message(chat_id=chat_id, sender="assistant", message=reply)
                db.session.add(bot_msg)
                db.session.commit()
                return jsonify({"reply": reply, "chat_id": chat_id}), 200
            chat_session.contact_no = normalized_contact
            move_detail = chat_session.move_detail
            if move_detail:
                move_detail.contact_no = normalized_contact
            chat_session.state = ChatState.AWAITING_FINAL_CONFIRMATION
            db.session.commit()
            svc = move_detail.additional_services or ""
            svc_list = [s for s in svc.split(",") if s]
            svc_str = ", ".join(svc_list) if svc_list else "None"
            details = (
                f"Here are your move details:\n"
                f"📍 From: {move_detail.origin.title() if move_detail.origin else 'Not Provided'}\n"
                f"📍 To: {move_detail.destination.title() if move_detail.destination else 'Not Provided'}\n"
                f"🏠 Move Size: {move_detail.move_size.title() if move_detail.move_size else 'Not Provided'}\n"
                f"📅 Move Date: {move_detail.move_date}\n"
                f"🔧 Additional Services: {svc_str}\n"
                f"📧 Email: {move_detail.email or 'Not Provided'}\n"
                f"💰 Estimated Cost: ${chat_session.estimated_cost_min} - ${chat_session.estimated_cost_max}\n"
                f"👤 Name: {move_detail.username}\n"
                f"📞 Contact No: {move_detail.contact_no}\n\n"
                f"Do you confirm this booking? (Yes/No) 👍👎"
            )
            bot_msg = Message(chat_id=chat_id, sender="assistant", message=details)
            db.session.add(bot_msg)
            db.session.commit()
            return jsonify({"reply": details, "chat_id": chat_id}), 200
        elif chat_session.state == ChatState.AWAITING_FINAL_CONFIRMATION:
            if user_input.lower() in ["yes", "y", "👍"]:
                chat_session.confirmed = True
                chat_session.is_active = False
                chat_session.state = ChatState.CONFIRMED
                db.session.commit()
                final_msg = "Your move has been successfully confirmed! 🎉 Our team will be in touch soon."
                bot_msg = Message(chat_id=chat_id, sender="assistant", message=final_msg)
                db.session.add(bot_msg)
                db.session.commit()
                return jsonify({"reply": final_msg, "chat_id": chat_id}), 200
            elif user_input.lower() in ["no", "n", "👎"]:
                chat_session.state = ChatState.MODIFY_DETAILS
                db.session.commit()
                prompt = "I understand. Which details would you like to change? (e.g., new date, different origin/destination, etc.)"
                bot_msg = Message(chat_id=chat_id, sender="assistant", message=prompt)
                db.session.add(bot_msg)
                db.session.commit()
                return jsonify({"reply": prompt, "chat_id": chat_id}), 200
            else:
                reply = "Please respond with Yes or No. Do you confirm this booking?"
                bot_msg = Message(chat_id=chat_id, sender="assistant", message=reply)
                db.session.add(bot_msg)
                db.session.commit()
                return jsonify({"reply": reply, "chat_id": chat_id}), 200
        elif chat_session.state == ChatState.MODIFY_DETAILS:
            any_new_info, did_estimate, reply_message = collect_or_update_move_details(chat_session, user_input)
            if any_new_info:
                if reply_message:
                    bot_msg = Message(chat_id=chat_id, sender="assistant", message=reply_message)
                    db.session.add(bot_msg)
                    db.session.commit()
                    return jsonify({"reply": reply_message, "chat_id": chat_id}), 200
        fallback = normal_gpt_reply(chat_session, user_input)
        return jsonify({"reply": fallback, "chat_id": chat_id}), 200
    except Exception as e:
        logger.error(f"Error in /general_query: {e}")
        return jsonify({"error": "An internal error occurred. Please try again later."}), 500

@app.route("/calculate_distance", methods=["POST"])
def calculate_distance():
    data = request.get_json() or {}
    origin = data.get("origin")
    destination = data.get("destination")
    if not origin or not destination:
        return jsonify({"error": "Missing origin/destination"}), 400
    try:
        dist = maps_manager.calculate_distance(origin, destination)
        if dist is None:
            return jsonify({"error": "Unable to calculate distance."}), 400
        return jsonify({"distance": dist})
    except Exception as e:
        logger.error(f"Error calculating distance: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/estimate_cost", methods=["POST"])
def estimate_cost():
    data = request.get_json() or {}
    chat_id = data.get("chat_id") or str(uuid.uuid4())
    origin = data.get("origin")
    destination = data.get("destination")
    move_size = data.get("move_size")
    additional_services = data.get("additional_services", [])
    username = data.get("username", "Unknown")
    contact_no = data.get("contact_no", "Unknown")
    move_date = data.get("move_date", "Unknown")
    if not (origin and destination and move_size):
        return jsonify({"error": "Missing required fields (origin, destination, move_size)."}), 400
    try:
        dist, cost_range = maps_manager.estimate_cost(
            origin, destination, move_size, additional_services, move_date
        )
        if dist is None:
            return jsonify({"error": cost_range}), 400
        min_cost, max_cost = cost_range
        estimated_cost = f"${min_cost} - ${max_cost}"
        chat_session = ChatSession.query.filter_by(chat_id=chat_id).first()
        if not chat_session:
            chat_session = ChatSession(
                chat_id=chat_id, username=username, contact_no=contact_no,
                move_date=move_date, estimated_cost_min=min_cost,
                estimated_cost_max=max_cost, state=ChatState.COST_ESTIMATED
            )
        else:
            chat_session.estimated_cost_min = min_cost
            chat_session.estimated_cost_max = max_cost
            chat_session.move_date = move_date
            chat_session.state = ChatState.COST_ESTIMATED
        db.session.add(chat_session)
        db.session.commit()
        move_detail = MoveDetail.query.filter_by(chat_id=chat_id).first()
        if not move_detail:
            move_detail = MoveDetail(chat_id=chat_id)
        move_detail.origin = origin
        move_detail.destination = destination
        move_detail.move_size = move_size
        move_detail.additional_services = ",".join(additional_services)
        move_detail.username = username
        move_detail.contact_no = contact_no
        move_detail.move_date = move_date
        move_detail.estimated_cost_min = min_cost
        move_detail.estimated_cost_max = max_cost
        db.session.add(move_detail)
        db.session.commit()
        return jsonify({"estimated_cost": estimated_cost, "chat_id": chat_id}), 200
    except Exception as e:
        logger.error(f"Error estimating cost: {e}")
        return jsonify({"error": str(e)}), 500

##############################################
# RUN
##############################################
if __name__ == "__main__":
    from apscheduler.schedulers.background import BackgroundScheduler

    def deactivate_inactive_sessions():
        cutoff = datetime.utcnow() - timedelta(hours=24)
        inactive = ChatSession.query.filter(
            ChatSession.created_at < cutoff,
            ChatSession.is_active == True
        ).all()
        for s in inactive:
            s.is_active = False
        db.session.commit()
        logger.info(f"Deactivated {len(inactive)} inactive sessions.")

    scheduler = BackgroundScheduler()
    scheduler.add_job(func=deactivate_inactive_sessions, trigger="interval", hours=1)
    scheduler.start()

    try:
        app.run(host="0.0.0.0", port=5001, debug=True)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
