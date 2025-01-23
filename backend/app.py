# app.py

import os
import uuid
import re
import json
from datetime import datetime

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from dateutil import parser
import logging

# Managers (Ensure these are correctly implemented in separate modules)
from .openai_manager import OpenAIManager
from .maps_manager import MapsManager
from .faq_manager import FAQManager

##############################################
# FLASK APP SETUP + CORS + SQLALCHEMY CONFIG
##############################################
app = Flask(__name__)
# Allow all origins for development
CORS(app, resources={r"/*": {"origins": "*"}})

# Configuration
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(os.getcwd(), "chatbot.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

##############################################
# DATABASE MODELS
##############################################

class ChatState:
    INITIAL = "INITIAL"
    COST_ESTIMATED = "COST_ESTIMATED"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    AWAITING_DETAILS = "AWAITING_DETAILS"
    AWAITING_FINAL_CONFIRMATION = "AWAITING_FINAL_CONFIRMATION"
    MODIFY_DETAILS = "MODIFY_DETAILS"  # New State
    CONFIRMED = "CONFIRMED"

class ChatSession(db.Model):
    """
    Represents a chat session.
    """
    __tablename__ = "chat_sessions"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    chat_id = db.Column(db.String(100), unique=True, nullable=False)
    username = db.Column(db.String(100), nullable=True)  # Changed to nullable
    contact_no = db.Column(db.String(50), nullable=True)  # Changed to nullable
    move_date = db.Column(db.String(100), nullable=True)
    estimated_cost_min = db.Column(db.Float, nullable=True)
    estimated_cost_max = db.Column(db.Float, nullable=True)
    confirmed = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)  # New Column
    state = db.Column(db.String(50), default=ChatState.INITIAL)  # New Field
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    messages = db.relationship("Message", backref="chat_session", lazy=True)
    move_detail = db.relationship(
        "MoveDetail",
        backref="chat_session",
        uselist=False
    )

    def __repr__(self):
        return f"<ChatSession {self.chat_id}>"

class Message(db.Model):
    """
    Represents an individual message within a chat session.
    """
    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    chat_id = db.Column(
        db.String(100),
        db.ForeignKey("chat_sessions.chat_id"),
        nullable=False
    )
    sender = db.Column(db.String(20), nullable=False)  # 'user' or 'assistant'
    message = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Message {self.id} from {self.sender}>"

class MoveDetail(db.Model):
    """
    Represents move details extracted from user inputs.
    """
    __tablename__ = "move_details"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    chat_id = db.Column(
        db.String(100),
        db.ForeignKey("chat_sessions.chat_id"),
        unique=True,
        nullable=False
    )
    origin = db.Column(db.String(100), nullable=True)
    destination = db.Column(db.String(100), nullable=True)
    move_size = db.Column(db.String(100), nullable=True)
    additional_services = db.Column(db.String(200), nullable=True)
    # Removed move_date from here
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
# Adjust the path to your FAQ dataset
faq_manager.load_faqs("/backend/data/faqs.jsonl")

##############################################
# LOGGER CONFIGURATION
##############################################
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

##############################################
# HELPER FUNCTIONS
##############################################
def is_move_request(user_text):
    """
    Determines if the user's message is likely a move request by checking for key phrases and patterns.
    """
    move_keywords = [
        "move from",
        "moving from",
        "relocate from",
        "relocating from",
        "shift from",
        "shifting from",
        "transport from",
        "transporting from"
    ]
    
    # Check if any of the move keywords are in the text
    has_move_keyword = any(keyword in user_text.lower() for keyword in move_keywords)
    
    # Basic check for location patterns (e.g., "from X to Y")
    has_location_pattern = bool(re.search(r'from\s+\w+\s+to\s+\w+', user_text.lower()))
    
    return has_move_keyword or has_location_pattern

def create_short_system_prompt():
    """
    Return a system prompt instructing the model to be concise and use emoticons.
    """
    return (
        "You are MoveBot 🤖, a friendly assistant for My Good Movers. "
        "My Good Movers is a platform that connects users and moving companies. "
        "Try to convince the user to take our services. "
        "Use emoticons to make your responses more friendly and engaging. "
        "Keep your answers brief, no more than 2 short sentences."
    )

def is_faq_query(user_text):
    """
    Determines if the user query is likely an FAQ based on keywords.
    """
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
    for kw in keywords:
        if kw in user_text.lower():
            return True
    return False

def standardize_date(date_str):
    """
    Converts a date string into a standard YYYY-MM-DD format.
    """
    try:
        parsed_date = parser.parse(date_str, fuzzy=True)
        return parsed_date.strftime("%Y-%m-%d")
    except Exception as e:
        logger.error(f"Date Parsing Error: {e}")
        return None  # Return None if parsing fails

def parse_move_details_with_openai(user_text):
    """
    Enhanced parsing function with validation for move_date.
    """
    system_prompt = (
        "You are a JSON parser for a moving service chatbot. The user may provide details about their move.\n"
        "Extract the following information into JSON with these exact keys: origin, destination, move_size, move_date, additional_services, username, contact_no.\n"
        "If a field is not mentioned, set it to null or an empty array as appropriate.\n"
        "Ensure that 'move_date' captures the date of the move in a clear format (e.g., '31st March', 'March 31', '2025-03-31').\n"
        "Here are some examples of user inputs and the expected JSON outputs:\n"
        "\n"
        "Example 1:\n"
        "User: I am moving from New York to Vegas with a 2-bedroom apartment on 31st March.\n"
        "JSON Output: {\n"
        "  \"origin\": \"New York\",\n"
        "  \"destination\": \"Vegas\",\n"
        "  \"move_size\": \"2-bedroom\",\n"
        "  \"move_date\": \"31st March\",\n"
        "  \"additional_services\": [],\n"
        "  \"username\": null,\n"
        "  \"contact_no\": null\n"
        "}\n"
        "\n"
        "Example 2:\n"
        "User: My name is John and my contact number is 555-1234. I need help moving from Los Angeles to San Francisco on March 15.\n"
        "JSON Output: {\n"
        "  \"origin\": \"Los Angeles\",\n"
        "  \"destination\": \"San Francisco\",\n"
        "  \"move_size\": null,\n"
        "  \"move_date\": \"March 15\",\n"
        "  \"additional_services\": [],\n"
        "  \"username\": \"John\",\n"
        "  \"contact_no\": \"555-1234\"\n"
        "}\n"
        "\n"
        "Example 3:\n"
        "User: I am relocating from Chicago to Houston with a 3-bedroom house on 2025-04-20. I need packing and storage services.\n"
        "JSON Output: {\n"
        "  \"origin\": \"Chicago\",\n"
        "  \"destination\": \"Houston\",\n"
        "  \"move_size\": \"3-bedroom\",\n"
        "  \"move_date\": \"2025-04-20\",\n"
        "  \"additional_services\": [\"packing\", \"storage\"],\n"
        "  \"username\": null,\n"
        "  \"contact_no\": null\n"
        "}\n"
        "\n"
        "Example 4:\n"
        "User: Moving from Boston to Miami, need a studio on April 5th, name Jane Doe, contact 123-4567.\n"
        "JSON Output: {\n"
        "  \"origin\": \"Boston\",\n"
        "  \"destination\": \"Miami\",\n"
        "  \"move_size\": \"studio\",\n"
        "  \"move_date\": \"April 5th\",\n"
        "  \"additional_services\": [],\n"
        "  \"username\": \"Jane Doe\",\n"
        "  \"contact_no\": \"123-4567\"\n"
        "}\n"
        "\n"
        "Return only valid JSON with these keys, no additional text or formatting."
    )

    # Call the extraction method from OpenAIManager
    extraction_response = openai_manager.extract_fields_from_text(
        system_prompt, user_text
    )
    logger.debug(f"OpenAI Extraction Response: {extraction_response}")  # Debugging

    try:
        parsed_data = json.loads(extraction_response)
        logger.debug(f"Parsed Data: {parsed_data}")  # Debugging

        move_date = parsed_data.get("move_date")
        if move_date:
            standardized_date = standardize_date(move_date)
            if standardized_date:
                parsed_data["move_date"] = standardized_date
            else:
                parsed_data["move_date"] = None  # Invalid date, set to None

        return {
            "origin": parsed_data.get("origin"),
            "destination": parsed_data.get("destination"),
            "move_size": parsed_data.get("move_size"),
            "move_date": parsed_data.get("move_date"),  # May be None
            "additional_services": parsed_data.get("additional_services") or [],
            # "username": parsed_data.get("username"),
            # "contact_no": parsed_data.get("contact_no")
        }
    except json.JSONDecodeError as e:
        logger.error(f"JSON Decode Error: {e}")
        # Fallback if parse fails
        return {
            "origin": None,
            "destination": None,
            "move_size": None,
            "move_date": None,  # Ensure move_date is present
            "additional_services": [],
            # "username": None,
            # "contact_no": None
        }

def normal_gpt_reply(chat_session, user_text):
    """
    If it's neither cost nor FAQ, do a normal GPT approach.
    """
    system_prompt = create_short_system_prompt()
    combined_input = f"Chat History:\n{get_chat_history(chat_session.chat_id)}\nUser: {user_text}"

    gpt_response = openai_manager.get_general_response(
        system_content=system_prompt,
        user_content=combined_input
    )

    # Store the assistant's reply
    assistant_message = Message(
        chat_id=chat_session.chat_id,
        sender="assistant",
        message=gpt_response
    )
    db.session.add(assistant_message)
    db.session.commit()

    return gpt_response

def get_chat_history(chat_id):
    """
    Retrieves the entire chat history for a given chat_id.
    """
    messages = Message.query.filter_by(
        chat_id=chat_id).order_by(
        Message.timestamp).all()
    history = "\n".join([f"{msg.sender.capitalize()}: {msg.message}" for msg in messages])
    return history

##############################################
# FLASK ROUTES
##############################################

@app.route("/", methods=["GET"])
def home():
    return render_template('index.html')

@app.route("/start_chat", methods=["POST"])
def start_chat_route():
    try:
        # Generate a unique chat_id
        chat_id = str(uuid.uuid4())
        logger.info(f"Starting new chat session with chat_id: {chat_id}")

        # Create a new ChatSession with INITIAL state
        chat_session = ChatSession(
            chat_id=chat_id,
            state=ChatState.INITIAL
        )
        db.session.add(chat_session)
        db.session.commit()

        # Create a welcome message
        welcome_message = "Hello! I'm MoveBot 🤖. How can I assist you with your move today? 📦🚚"
        welcome = Message(
            chat_id=chat_id,
            sender="assistant",
            message=welcome_message
        )
        db.session.add(welcome)
        db.session.commit()

        logger.info(f"Chat session {chat_id} initialized successfully.")

        return jsonify({
            "chat_id": chat_id,
            "message": welcome_message
        }), 200

    except Exception as e:
        logger.error(f"Error in /start_chat: {e}")
        return jsonify({"error": "Failed to start chat session."}), 500

@app.route("/end_chat", methods=["POST"])
def end_chat():
    """
    Ends chat, store final in DB.
    """
    try:
        data = request.get_json() or {}
        chat_id = data.get("chat_id")
        if not chat_id:
            return jsonify({"error": "Missing chat_id"}), 400

        # Retrieve ChatSession
        chat_session = ChatSession.query.filter_by(chat_id=chat_id).first()
        if not chat_session:
            return jsonify({"error": "Chat session not found."}), 404

        # Store the bot's farewell message
        farewell_message = "Chat ended successfully. Thank you for using My Good Movers! 👋"
        farewell = Message(
            chat_id=chat_id,
            sender="assistant",
            message=farewell_message
        )
        db.session.add(farewell)

        # Deactivate the chat session
        chat_session.is_active = False
        db.session.add(chat_session)
        db.session.commit()

        logger.info(f"Chat session {chat_id} ended by user.")

        return jsonify({"message": farewell_message}), 200

    except Exception as e:
        logger.error(f"Error in /end_chat: {e}")
        return jsonify(
            {"error": "An error occurred while ending the chat."}), 500

@app.route("/general_query", methods=["POST"])
def general_query_route():
    try:
        data = request.get_json() or {}
        user_input = data.get("message", "").strip()
        chat_id = data.get("chat_id")

        if not chat_id:
            return jsonify({"error": "Missing chat_id"}), 400

        # Retrieve ChatSession
        chat_session = ChatSession.query.filter_by(chat_id=chat_id).first()
        if not chat_session:
            return jsonify({"error": "Chat session not found."}), 404

        if not chat_session.is_active and chat_session.state != ChatState.CONFIRMED:
            return jsonify({"error": "This chat session has been ended. Please start a new session."}), 400

        # Store user message
        user_message = Message(
            chat_id=chat_id,
            sender="user",
            message=user_input
        )
        db.session.add(user_message)
        db.session.commit()

        logger.info(f"Received message from user in chat_id {chat_id}: {user_input}")

        # Handle based on current state
        current_state = chat_session.state

        # INITIAL state handling
        if current_state == ChatState.INITIAL:
            # First check if it's a FAQ query
            if is_faq_query(user_input):
                answer = faq_manager.find_best_match(user_input)
                assistant_message = Message(
                    chat_id=chat_id,
                    sender="assistant",
                    message=answer
                )
                db.session.add(assistant_message)
                db.session.commit()
                return jsonify({"reply": answer, "chat_id": chat_id}), 200
            
            # Then check if it's a move request
            if is_move_request(user_input):
                # Parse move details
                extracted = parse_move_details_with_openai(user_input)
                
                # Update MoveDetail
                move_detail = chat_session.move_detail
                if not move_detail:
                    move_detail = MoveDetail(chat_id=chat_id)
                move_detail.origin = extracted.get("origin") or move_detail.origin
                move_detail.destination = extracted.get("destination") or move_detail.destination
                move_detail.move_size = extracted.get("move_size") or move_detail.move_size
                move_detail.additional_services = ",".join(extracted.get("additional_services")) if extracted.get("additional_services") else move_detail.additional_services
                
                if extracted.get("move_date"):
                    chat_session.move_date = extracted.get("move_date")
                
                db.session.add(move_detail)
                db.session.commit()

                # Check if we have enough info for cost estimate
                if move_detail.origin and move_detail.destination and move_detail.move_size and chat_session.move_date:
                    # Calculate cost
                    distance, cost_range = maps_manager.estimate_cost(
                        move_detail.origin,
                        move_detail.destination,
                        move_detail.move_size,
                        move_detail.additional_services.split(',') if move_detail.additional_services else []
                    )
                    
                    if distance is not None:
                        min_cost, max_cost = cost_range
                        chat_session.estimated_cost_min = min_cost
                        chat_session.estimated_cost_max = max_cost
                        chat_session.state = ChatState.COST_ESTIMATED
                        db.session.commit()

                        estimate_reply = (
                            f"The estimated cost for moving from {move_detail.origin.title()} to {move_detail.destination.title()} "
                            f"({move_detail.move_size.title()}, date: {chat_session.move_date}) is between ${min_cost} and ${max_cost}. 🏠📦💰\n\n"
                            f"Would you like to proceed with booking this move? (Reply with Yes/No) 👍👎"
                        )

                        assistant_message = Message(
                            chat_id=chat_id,
                            sender="assistant",
                            message=estimate_reply
                        )
                        db.session.add(assistant_message)
                        db.session.commit()

                        return jsonify({"reply": estimate_reply, "chat_id": chat_id}), 200
                
                # If we don't have enough info, ask for missing details
                missing_fields = []
                if not move_detail.origin:
                    missing_fields.append("origin location")
                if not move_detail.destination:
                    missing_fields.append("destination")
                if not move_detail.move_size:
                    missing_fields.append("move size")
                if not chat_session.move_date:
                    missing_fields.append("move date")

                if missing_fields:
                    missing_fields_str = ", ".join(missing_fields[:-1]) + (" and " if len(missing_fields) > 1 else "") + missing_fields[-1]
                    reply = f"To provide you with an accurate cost estimate, I need your {missing_fields_str}. Please provide these details. 📝"
                    
                    assistant_message = Message(
                        chat_id=chat_id,
                        sender="assistant",
                        message=reply
                    )
                    db.session.add(assistant_message)
                    db.session.commit()

                    return jsonify({"reply": reply, "chat_id": chat_id}), 200
            
            # If it's not a move request or FAQ, handle as general query
            return jsonify({"reply": normal_gpt_reply(chat_session, user_input), "chat_id": chat_id}), 200

        # Handle state after cost estimate is provided
        elif current_state == ChatState.COST_ESTIMATED:
            if user_input.lower() in ["yes", "y", "👍"]:
                chat_session.state = ChatState.AWAITING_DETAILS
                db.session.commit()
                
                reply = "Great! To confirm your booking, I'll need your name and contact number. Please provide them in this format: John Doe, 555-1234 📇"
                
                assistant_message = Message(
                    chat_id=chat_id,
                    sender="assistant",
                    message=reply
                )
                db.session.add(assistant_message)
                db.session.commit()
                
                return jsonify({"reply": reply, "chat_id": chat_id}), 200
            
            elif user_input.lower() in ["no", "n", "👎"]:
                chat_session.state = ChatState.INITIAL
                reply = "No problem! Let me know if you'd like to get another estimate or if you have any questions. 😊"
                
                assistant_message = Message(
                    chat_id=chat_id,
                    sender="assistant",
                    message=reply
                )
                db.session.add(assistant_message)
                db.session.commit()
                
                return jsonify({"reply": reply, "chat_id": chat_id}), 200
            
            else:
                reply = "Please respond with 'Yes' or 'No'. Would you like to proceed with booking this move? 👍👎"
                assistant_message = Message(
                    chat_id=chat_id,
                    sender="assistant",
                    message=reply
                )
                db.session.add(assistant_message)
                db.session.commit()
                return jsonify({"reply": reply, "chat_id": chat_id}), 200

        # Handle user details collection
        elif current_state == ChatState.AWAITING_DETAILS:
            name_contact = user_input.split(",")
            if len(name_contact) < 2:
                prompt = "Please provide both your name and contact number, separated by a comma. For example: John Doe, 555-1234 📇"
                assistant_message = Message(
                    chat_id=chat_id,
                    sender="assistant",
                    message=prompt
                )
                db.session.add(assistant_message)
                db.session.commit()
                return jsonify({"reply": prompt, "chat_id": chat_id}), 200

            name = name_contact[0].strip()
            contact_no = name_contact[1].strip()

            # Update ChatSession with user details
            chat_session.username = name
            chat_session.contact_no = contact_no
            chat_session.state = ChatState.AWAITING_FINAL_CONFIRMATION
            db.session.commit()

            # Display all details for final confirmation
            move_detail = chat_session.move_detail
            if not move_detail:
                return jsonify({"error": "Move details not found."}), 400

            details = (
                f"Here are your move details:\n"
                f"📍 <strong>From:</strong> {move_detail.origin.title() if move_detail.origin else 'Not Provided'}\n"
                f"📍 <strong>To:</strong> {move_detail.destination.title() if move_detail.destination else 'Not Provided'}\n"
                f"🏠 <strong>Move Size:</strong> {move_detail.move_size.title() if move_detail.move_size else 'Not Provided'}\n"
                f"📅 <strong>Move Date:</strong> {chat_session.move_date}\n"
                f"💰 <strong>Estimated Cost:</strong> ${chat_session.estimated_cost_min} - ${chat_session.estimated_cost_max}\n"
                f"👤 <strong>Name:</strong> {chat_session.username}\n"
                f"📞 <strong>Contact No:</strong> {chat_session.contact_no}\n\n"
                f"Please review your details. Do you confirm this booking? (Yes/No) 👍👎"
            )

            assistant_message = Message(
                chat_id=chat_id,
                sender="assistant",
                message=details
            )
            db.session.add(assistant_message)
            db.session.commit()

            return jsonify({"reply": details, "chat_id": chat_id}), 200

        # Handle final confirmation
        elif current_state == ChatState.AWAITING_FINAL_CONFIRMATION:
            if user_input.lower() in ["yes", "y", "👍"]:
                chat_session.confirmed = True
                chat_session.state = ChatState.CONFIRMED
                chat_session.is_active = False
                db.session.commit()

                confirmation_message = "Your move has been successfully confirmed! 🎉 Our team will reach out to you shortly. Thank you for choosing My Good Movers! 😊"
                assistant_message = Message(
                    chat_id=chat_id,
                    sender="assistant",
                    message=confirmation_message
                )
                db.session.add(assistant_message)
                db.session.commit()

                return jsonify({"reply": confirmation_message, "chat_id": chat_id}), 200

            elif user_input.lower() in ["no", "n", "👎"]:
                chat_session.state = ChatState.MODIFY_DETAILS
                reply = "I understand. What details would you like to modify? Please provide the updated information."
                assistant_message = Message(
                    chat_id=chat_id,
                    sender="assistant",
                    message=reply
                )
                db.session.add(assistant_message)
                db.session.commit()

                return jsonify({"reply": reply, "chat_id": chat_id}), 200

            else:
                reply = "Please respond with 'Yes' or 'No'. Do you confirm this booking? 👍👎"
                assistant_message = Message(
                    chat_id=chat_id,
                    sender="assistant",
                    message=reply
                )
                db.session.add(assistant_message)
                db.session.commit()
                return jsonify({"reply": reply, "chat_id": chat_id}), 200

        # Handle modifications
        elif current_state == ChatState.MODIFY_DETAILS:
            # Parse the modification request
            extracted = parse_move_details_with_openai(user_input)
            move_detail = chat_session.move_detail
            
            if extracted.get("origin"):
                move_detail.origin = extracted["origin"]
            if extracted.get("destination"):
                move_detail.destination = extracted["destination"]
            if extracted.get("move_size"):
                move_detail.move_size = extracted["move_size"]
            if extracted.get("move_date"):
                chat_session.move_date = extracted["move_date"]
            if extracted.get("additional_services"):
                move_detail.additional_services = ",".join(extracted["additional_services"])
                
            # Recalculate cost if necessary
            if any([extracted.get(field) for field in ["origin", "destination", "move_size"]]):
                distance, cost_range = maps_manager.estimate_cost(
                    move_detail.origin,
                    move_detail.destination,
                    move_detail.move_size,
                    move_detail.additional_services.split(',') if move_detail.additional_services else []
                )
                if distance is not None:
                    chat_session.estimated_cost_min, chat_session.estimated_cost_max = cost_range

            db.session.commit()
            
            # Show updated details
            details = (
                f"Here are your updated move details:\n"
                f"📍 <strong>From:</strong> {move_detail.origin.title() if move_detail.origin else 'Not Provided'}\n"
                f"📍 <strong>To:</strong> {move_detail.destination.title() if move_detail.destination else 'Not Provided'}\n"
                f"🏠 <strong>Move Size:</strong> {move_detail.move_size.title() if move_detail.move_size else 'Not Provided'}\n"
                f"📅 <strong>Move Date:</strong> {chat_session.move_date}\n"
                f"💰 <strong>Estimated Cost:</strong> ${chat_session.estimated_cost_min} - ${chat_session.estimated_cost_max}\n"
                f"👤 <strong>Name:</strong> {chat_session.username}\n"
                f"📞 <strong>Contact No:</strong> {chat_session.contact_no}\n\n"
                f"Please review your updated details. Do you confirm this booking? (Yes/No) 👍👎"
            )
            
            chat_session.state = ChatState.AWAITING_FINAL_CONFIRMATION
            db.session.commit()
            
            assistant_message = Message(
                chat_id=chat_id,
                sender="assistant",
                message=details
            )
            db.session.add(assistant_message)
            db.session.commit()
            
            return jsonify({"reply": details, "chat_id": chat_id}), 200

        # Handle FAQ or general queries in any other state
        else:
            if is_faq_query(user_input):
                answer = faq_manager.find_best_match(user_input)
                assistant_message = Message(
                    chat_id=chat_id,
                    sender="assistant",
                    message=answer
                )
                db.session.add(assistant_message)
                db.session.commit()
                return jsonify({"reply": answer, "chat_id": chat_id}), 200

            return jsonify({"reply": normal_gpt_reply(chat_session, user_input), "chat_id": chat_id}), 200

    except Exception as e:
        logger.error(f"Error in /general_query:")
##############################################
# (Optional) Other endpoints
##############################################

@app.route("/calculate_distance", methods=["POST"])
def calculate_distance():
    data = request.get_json() or {}
    origin = data.get("origin")
    destination = data.get("destination")

    if not origin or not destination:
        return jsonify({"error": "Missing origin/destination"}), 400

    try:
        distance = maps_manager.calculate_distance(origin, destination)
        if distance is None:
            return jsonify(
                {"error": "Unable to calculate distance."}), 400
        return jsonify({"distance": distance})
    except Exception as e:
        logger.error(f"Error calculating distance: {e}")
        return jsonify({"error": f"An error occurred: {e}"}), 500

@app.route("/estimate_cost", methods=["POST"])
def estimate_cost():
    """
    Direct approach if you want a separate route to pass origin/dest/size.
    But we mainly handle cost in general_query above.
    """
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
        return jsonify(
            {"error": "Missing required fields."}), 400

    try:
        distance, cost_range_or_error = maps_manager.estimate_cost(
            origin, destination, move_size, additional_services
        )
        if distance is None:
            return jsonify({"error": cost_range_or_error}), 400

        min_cost, max_cost = cost_range_or_error
        estimated_cost = f"${min_cost} - ${max_cost}"

        # Create or update ChatSession
        chat_session = ChatSession.query.filter_by(chat_id=chat_id).first()
        if not chat_session:
            chat_session = ChatSession(
                chat_id=chat_id,
                username=username,
                contact_no=contact_no
            )
        chat_session.estimated_cost_min = min_cost
        chat_session.estimated_cost_max = max_cost
        chat_session.move_date = move_date
        db.session.add(chat_session)
        db.session.commit()

        # Create or update MoveDetail
        move_detail = MoveDetail.query.filter_by(chat_id=chat_id).first()
        if not move_detail:
            move_detail = MoveDetail(chat_id=chat_id)
        move_detail.origin = origin
        move_detail.destination = destination
        move_detail.move_size = move_size
        move_detail.additional_services = ",".join(additional_services)
        db.session.add(move_detail)
        db.session.commit()

        return jsonify(
            {"estimated_cost": estimated_cost, "chat_id": chat_id}), 200
    except Exception as e:
        logger.error(f"Error estimating cost: {e}")
        return jsonify({"error": f"An error occurred: {e}"}), 500

##############################################
# RUN THE APP
##############################################
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
