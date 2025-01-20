import os
import uuid
import re
import json
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from dateutil import parser

# Managers
from redis_manager import RedisManager
from openai_manager import OpenAIManager
from maps_manager import MapsManager
from faq_manager import FAQManager

##############################################
# FLASK APP SETUP + CORS + SQLITE CONFIG
##############################################
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(os.getcwd(), "chatbot.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)

##############################################
# DATABASE MODEL: ChatRecord for storing ended chats
##############################################
class ChatRecord(db.Model):
    """
    Stores completed chatbot sessions. Each record corresponds to
    one 'ended' chat, containing:
      - chat_id (matches Redis)
      - username
      - contact_no
      - messages (the entire transcript)
      - created_at (timestamp)
    """
    __tablename__ = "chat_records"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    chat_id = db.Column(db.String(100), nullable=False)
    username = db.Column(db.String(100), default="Unknown")
    contact_no = db.Column(db.String(50), default="Unknown")
    messages = db.Column(db.Text)  # entire conversation transcript
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<ChatRecord {self.chat_id}>"


with app.app_context():
    db.create_all()

##############################################
# INIT MANAGERS
##############################################
redis_manager = RedisManager()
openai_manager = OpenAIManager()
maps_manager = MapsManager()
faq_manager = FAQManager()
# Adjust the path to your FAQ dataset
faq_manager.load_faqs("/Users/TalhaZain/chatbot_html/backend/data/faqs.jsonl")

##############################################
# HELPER: system prompt for short GPT replies
##############################################
def create_short_system_prompt():
    """
    Return a system prompt instructing the model to be concise.
    """
    return (
        "You are a helpful assistant for My Good Movers. "
        "My Good Movers is a platform that connects users and moving companies. "
        "Try to convince the user to take our services. "
        "Keep your answers brief, no more than 2 short sentences."
    )

##############################################
# HELPER: Decide if user query is an FAQ
##############################################
def is_faq_query(user_text):
    """
    Determines if the user query is likely an FAQ based on keywords.
    """
    # You can enhance this with more sophisticated NLP techniques or use semantic similarity
    keywords = ["modify booking", "hidden charge", "refund", "cancel", "policy", "charges", "payment", "change booking"]
    for kw in keywords:
        if kw in user_text.lower():
            return True
    return False

##############################################
# HELPER: Let OpenAI parse the user's text for move fields
##############################################
def standardize_date(date_str):
    """
    Converts a date string into a standard YYYY-MM-DD format.
    """
    try:
        parsed_date = parser.parse(date_str, fuzzy=True)
        return parsed_date.strftime("%Y-%m-%d")
    except Exception as e:
        print(f"[ERROR] Date Parsing Error: {e}")
        return date_str  # Return as-is if parsing fails

def is_valid_contact(contact_no):
    """
    Validates the contact number using regex.
    Accepts formats like '555-1234', '5551234', '+1-555-1234', etc.
    """
    pattern = r'^(\+\d{1,2}\s?)?(\d{3}[-.\s]?){2}\d{4}$'
    return re.match(pattern, contact_no) is not None

# def parse_move_date_fallback(user_text):
#     """
#     Attempts to parse a date from the user input using dateutil.
#     Returns a standardized date string or None if parsing fails.
#     """
#     try:
#         parsed_date = parser.parse(user_text, fuzzy=True)
#         return parsed_date.strftime("%Y-%m-%d")
#     except Exception as e:
#         print(f"[ERROR] Date Parsing Fallback Error: {e}")
#         return None

def parse_move_details_with_openai(user_text):
    """
    Enhanced parsing function with fallback for move_date.
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
    extraction_response = openai_manager.extract_fields_from_text(system_prompt, user_text)
    print(f"[DEBUG] OpenAI Extraction Response: {extraction_response}")  # Debugging

    try:
        parsed_data = json.loads(extraction_response)
        print(f"[DEBUG] Parsed Data: {parsed_data}")  # Debugging

        move_date = parsed_data.get("move_date")
        if not move_date:
            # Attempt to parse move_date using fallback
            move_date = standardize_date(user_text)
            parsed_data["move_date"] = move_date

        return {
            "origin": parsed_data.get("origin"),
            "destination": parsed_data.get("destination"),
            "move_size": parsed_data.get("move_size"),
            "move_date": parsed_data.get("move_date"),  # Added move_date
            "additional_services": parsed_data.get("additional_services") or [],
            "username": parsed_data.get("username"),
            "contact_no": parsed_data.get("contact_no")
        }
    except json.JSONDecodeError as e:
        print(f"[ERROR] JSON Decode Error: {e}")
        # Fallback if parse fails
        return {
            "origin": None,
            "destination": None,
            "move_size": None,
            "move_date": None,  # Ensure move_date is present
            "additional_services": [],
            "username": None,
            "contact_no": None
        }
##############################################
# HELPER: normal GPT fallback
##############################################
def normal_gpt_reply(chat_id, user_text):
    """
    If we decide it's neither cost nor FAQ, do a normal GPT approach.
    """
    prev_context = redis_manager.get_context(chat_id, "context") or ""
    system_prompt = create_short_system_prompt()
    combined_input = f"{prev_context}\nUser: {user_text}"

    gpt_response = openai_manager.get_general_response(
        system_content=system_prompt,
        user_content=combined_input
    )
    updated_context = f"{combined_input}\nAssistant: {gpt_response}"
    redis_manager.update_context(chat_id, "context", updated_context)

    # Append to chat
    redis_manager.add_message(chat_id, user_text, gpt_response)
    return gpt_response

##############################################
# FLASK ROUTES
##############################################
@app.route("/home", methods=["GET"])
def home():
    return "Chatbot API is running."

@app.route("/start_chat", methods=["POST"])
def start_chat():
    chat_id = str(uuid.uuid4())
    data = request.json or {}
    username = data.get("username", "Unknown")
    contact_no = data.get("contact_no", "Unknown")

    redis_manager.init_chat(chat_id, username=username, contact_no=contact_no)
    return jsonify({"chat_id": chat_id, "message": "New chat started."})

@app.route("/end_chat", methods=["POST"])
def end_chat():
    """
    Ends chat, store final in DB.
    """
    data = request.json or {}
    chat_id = data.get("chat_id")
    if not chat_id:
        return jsonify({"error": "Missing chat_id"}), 400

    try:
        # Get entire chat history from Redis
        chat_history = redis_manager.get_chat_history(chat_id)
        # Get move details
        move_details = redis_manager.get_move_details(chat_id)

        # Combine messages
        transcript = "\n".join(chat_history["messages"])
        username = move_details.get("username", "Unknown")
        contact_no = move_details.get("contact_no", "Unknown")

        # Store in SQLite
        record = ChatRecord(
            chat_id=chat_id,
            username=username,
            contact_no=contact_no,
            messages=transcript
        )
        db.session.add(record)
        db.session.commit()

        # Mark chat finished
        redis_manager.finish_chat(chat_id)
        return jsonify({"message": f"Chat {chat_id} ended and stored in DB successfully."})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

@app.route("/general_query", methods=["POST"])
def general_query():
    data = request.json or {}
    user_input = data.get("message", "").strip()
    chat_id = data.get("chat_id") or str(uuid.uuid4())

    # 1) Initialize or retrieve session
    redis_manager.init_chat(chat_id)

    # 2) Store user message with placeholder
    redis_manager.add_message(
        chat_id,
        user_message=user_input,
        assistant_message="[Processing]",
        overwrite_last=True
    )

    # 3) Check if user might be asking an FAQ
    if is_faq_query(user_input):
        # Call FAQ manager
        answer = faq_manager.find_best_match(user_input)
        redis_manager.add_message(
            chat_id,
            user_message=user_input,
            assistant_message=answer,
            overwrite_last=True
        )
        return jsonify({"reply": answer, "chat_id": chat_id})

    # 4) Parse info with OpenAI
    extracted = parse_move_details_with_openai(user_input)
    print(f"[DEBUG] Extracted Data: {extracted}")  # Debugging

    # Merge with existing data in Redis
    # Update origin
    old_origin = redis_manager.get_context(chat_id, "origin")
    if extracted.get("origin") and not old_origin:
        redis_manager.update_context(chat_id, "origin", extracted["origin"].lower())

    # Update destination
    old_dest = redis_manager.get_context(chat_id, "destination")
    if extracted.get("destination") and not old_dest:
        redis_manager.update_context(chat_id, "destination", extracted["destination"].lower())

    # Update move_size
    old_size = redis_manager.get_context(chat_id, "move_size")
    if extracted.get("move_size") and not old_size:
        redis_manager.update_context(chat_id, "move_size", extracted["move_size"])

    # Update move_date
    old_move_date = redis_manager.get_context(chat_id, "move_date")
    if extracted.get("move_date") and not old_move_date:
        standardized_date = standardize_date(extracted["move_date"])
        redis_manager.update_context(chat_id, "move_date", standardized_date)

    # Update additional_services
    old_services = redis_manager.get_context(chat_id, "additional_services") or ""
    existing_svc = set(s.strip() for s in old_services.split(",")) if old_services else set()
    for svc in extracted.get("additional_services", []):
        if svc:  # Ensure no empty strings
            existing_svc.add(svc)
    new_svc_str = ",".join(s for s in existing_svc if s)
    redis_manager.update_context(chat_id, "additional_services", new_svc_str)

    # Update username
    if extracted.get("username"):
        redis_manager.update_context(chat_id, "username", extracted["username"])

    # Update contact_no with validation
    if extracted.get("contact_no"):
        redis_manager.update_context(chat_id, "contact_no", extracted["contact_no"])
        # return jsonify({"reply": reply, "chat_id": chat_id})

    # 5) If user is asking cost
    lower_in = user_input.lower()
    if any(kw in lower_in for kw in ["cost", "estimate", "quote", "how much", "price"]):
        # Retrieve final data
        origin = redis_manager.get_context(chat_id, "origin")
        destination = redis_manager.get_context(chat_id, "destination")
        move_size = redis_manager.get_context(chat_id, "move_size")
        move_date = redis_manager.get_context(chat_id, "move_date")
        add_svc_str = redis_manager.get_context(chat_id, "additional_services") or ""
        additional_services = [s for s in add_svc_str.split(",") if s]

        username = redis_manager.get_context(chat_id, "username")
        contact_no = redis_manager.get_context(chat_id, "contact_no")

        # Identify which fields are missing
        missing_fields = []
        if not origin:
            missing_fields.append("origin")
        if not destination:
            missing_fields.append("destination")
        if not move_size:
            missing_fields.append("move size")
        if not move_date:
            missing_fields.append("move date")
        # if not username or username == "Unknown":
        #     missing_fields.append("name")
        # if not contact_no or contact_no == "Unknown":
        #     missing_fields.append("contact number")

        if missing_fields:
            # Build a user-friendly reply based on how many fields are missing
            if len(missing_fields) == 1:
                reply = f"We still need your {missing_fields[0]}. Please provide it."
            elif len(missing_fields) == 2:
                reply = f"We still need your {missing_fields[0]} and {missing_fields[1]}. Please provide them."
            else:
                # More than two missing
                joined_fields = ", ".join(missing_fields[:-1]) + f", and {missing_fields[-1]}"
                reply = f"We still need your {joined_fields}. Please provide them."

            redis_manager.add_message(
                chat_id,
                user_message=user_input,
                assistant_message=reply,
                overwrite_last=True
            )
            return jsonify({"reply": reply, "chat_id": chat_id})

        # All required fields are present, proceed to calculate cost
        distance, cost_range = maps_manager.estimate_cost(
            origin,
            destination,
            move_size,
            additional_services
        )
        if distance is None:
            reply = f"Sorry, I couldn’t calculate the distance from {origin.title()} to {destination.title()}."
        else:
            min_cost, max_cost = cost_range
            reply = (
                f"The estimated cost for moving from {origin.title()} to {destination.title()} "
                f"({move_size.title()} is between ${min_cost} and ${max_cost}."
            )

        # Store the cost reply
        redis_manager.add_message(
            chat_id,
            user_message=user_input,
            assistant_message=reply,
            overwrite_last=True
        )

        # Prompt for name and contact if missing
        prompts = []
        if not username or username == "Unknown":
            prompts.append("Please provide your name.")
        if not contact_no or contact_no == "Unknown":
            if username and username != "Unknown":
                prompts.append(f"Thanks {username}, now please share your contact number.")
            else:
                prompts.append("Please provide your contact number.")

        if prompts:
            full_reply = reply + " " + " ".join(prompts)
            redis_manager.add_message(
                chat_id,
                user_message=user_input,
                assistant_message=full_reply,
                overwrite_last=True
            )
            return jsonify({"reply": full_reply, "chat_id": chat_id})

        return jsonify({"reply": reply, "chat_id": chat_id})
    else:
        # 6) Fallback normal GPT
        final_reply = normal_gpt_reply(chat_id, user_input)
        return jsonify({"reply": final_reply, "chat_id": chat_id})

##############################################
# (Optional) Other endpoints
##############################################
@app.route("/calculate_distance", methods=["POST"])
def calculate_distance():
    data = request.json or {}
    origin = data.get("origin")
    destination = data.get("destination")

    if not origin or not destination:
        return jsonify({"error": "Missing origin/destination"}), 400

    try:
        distance = maps_manager.calculate_distance(origin, destination)
        if distance is None:
            return jsonify({"error": "Unable to calculate distance. Check locations."}), 400
        return jsonify({"distance": distance})
    except Exception as e:
        return jsonify({"error": f"An error occurred: {e}"}), 500

@app.route("/estimate_cost", methods=["POST"])
def estimate_cost():
    """
    Direct approach if you want a separate route to pass origin/dest/size. 
    But we mainly handle cost in general_query above.
    """
    data = request.json or {}
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

    distance, cost_range_or_error = maps_manager.estimate_cost(origin, destination, move_size, additional_services)
    if distance is None:
        return jsonify({"error": cost_range_or_error}), 400

    min_cost, max_cost = cost_range_or_error
    estimated_cost = f"${min_cost} - ${max_cost}"

    move_details = {
        "name": username,
        "contact_no": contact_no,
        "origin": origin,
        "destination": destination,
        "date": move_date,
        "move_size": move_size,
        "additional_services": additional_services
    }
    redis_manager.store_move_details(chat_id, move_details, estimated_cost)

    return jsonify({"estimated_cost": estimated_cost, "chat_id": chat_id})

##############################################
# RUN THE APP
##############################################
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
