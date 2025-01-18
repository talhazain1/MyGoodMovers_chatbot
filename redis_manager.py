# redis_manager.py

import redis
from config import Config

class RedisManager:
    def __init__(self):
        """
        Initialize the Redis connection with credentials from config.
        """
        self.client = redis.StrictRedis(
            host=Config.REDIS_HOST,
            port=Config.REDIS_PORT,
            password=Config.REDIS_PASSWORD,
            decode_responses=True
        )

    def init_chat(self, chat_id, username=None, contact_no=None):
        """
        Initialize a new chat session in Redis if it doesn't already exist.
        Sets the username, contact_no, and message index to 0.

        :param chat_id: Unique identifier for this chat session.
        :param username: (Optional) The user's name, default "Unknown".
        :param contact_no: (Optional) The user's contact, default "Unknown".
        """
        chat_key = f"chat:{chat_id}"
        if not self.client.exists(chat_key):
            self.client.hset(chat_key, mapping={
                "username": username or "Unknown",
                "contact_no": contact_no or "Unknown",
                "index": 0,
                "status": "active"
            })

    def add_message(self, chat_id, user_message=None, assistant_message=None, overwrite_last=False):
        """
        Append or overwrite user/assistant messages in Redis for the given chat.

        :param chat_id: unique chat session ID
        :param user_message: The user's message text (optional)
        :param assistant_message: The bot's message text (optional)
        :param overwrite_last: If True, overwrite the last assistant message instead of appending
        """
        chat_key = f"chat:{chat_id}"
        index = int(self.client.hget(chat_key, "index") or 0)

        if overwrite_last:
            # Overwrite the last assistant message at index-1 (if it exists)
            if index > 0:
                last_assistant_index = index - 1
                if assistant_message is not None:
                    self.client.hset(chat_key, f"message:{last_assistant_index}", f"Assistant: {assistant_message}")
            else:
                # If there's nothing to overwrite, you might choose to append instead
                pass
            return

        # Otherwise, the default append behavior
        if user_message is not None:
            self.client.hset(chat_key, f"message:{index}", f"User: {user_message}")
            index += 1

        if assistant_message is not None:
            self.client.hset(chat_key, f"message:{index}", f"Assistant: {assistant_message}")
            index += 1

        # Update index
        self.client.hset(chat_key, "index", index)

    def get_chat_history(self, chat_id):
        """
        Retrieve the entire chat history for a given chat, 
        including username & contact info.

        :param chat_id: The chat session ID to look up.
        :return: Dict with username, contact_no, and a list of messages (strings).
        :raises ValueError: If the chat_id does not exist in Redis.
        """
        chat_key = f"chat:{chat_id}"
        if not self.client.exists(chat_key):
            raise ValueError(f"Chat ID {chat_id} does not exist.")

        username = self.client.hget(chat_key, "username")
        contact_no = self.client.hget(chat_key, "contact_no")
        index = int(self.client.hget(chat_key, "index") or 0)

        messages = []
        for i in range(index):
            msg = self.client.hget(chat_key, f"message:{i}")
            if msg:
                messages.append(msg)

        return {
            "username": username,
            "contact_no": contact_no,
            "messages": messages
        }

    def store_move_details(self, chat_id, move_details, estimated_cost):
        """
        Store or update move-related details for a given chat session.

        :param chat_id: Chat session ID.
        :param move_details: Dictionary with keys like 'name', 'contact_no',
                             'origin', 'destination', 'date', 'move_size',
                             'additional_services'.
        :param estimated_cost: Final or tentative cost estimate for the move.
        """
        chat_key = f"chat:{chat_id}"

        # Convert list of services to a comma-separated string if any
        services_str = ", ".join(move_details.get("additional_services", []))

        self.client.hset(chat_key, mapping={
            "username": move_details.get("name", "Unknown"),
            "contact_no": move_details.get("contact_no", "Unknown"),
            "origin": move_details.get("origin", "Unknown"),
            "destination": move_details.get("destination", "Unknown"),
            "move_date": str(move_details.get("date", "Unknown")),
            "move_size": move_details.get("move_size", "Unknown"),
            "additional_services": services_str,
            "estimated_cost": estimated_cost
        })

    def get_move_details(self, chat_id):
        """
        Retrieve all stored move details for a given chat session.

        :param chat_id: The ID of the chat session to query.
        :return: A dictionary with move details (origin, destination, date, etc.) 
                 plus 'username' and 'contact_no'.
        :raises ValueError: If the chat_id does not exist in Redis.
        """
        chat_key = f"chat:{chat_id}"
        if not self.client.exists(chat_key):
            raise ValueError(f"Chat ID {chat_id} does not exist.")

        return {
            "username": self.client.hget(chat_key, "username"),
            "contact_no": self.client.hget(chat_key, "contact_no"),
            "origin": self.client.hget(chat_key, "origin"),
            "destination": self.client.hget(chat_key, "destination"),
            "move_date": self.client.hget(chat_key, "move_date"),
            "move_size": self.client.hget(chat_key, "move_size"),
            "additional_services": self.client.hget(chat_key, "additional_services"),
            "estimated_cost": self.client.hget(chat_key, "estimated_cost"),
            "status": self.client.hget(chat_key, "status")
        }

    def store_user_info(self, chat_id, username, contact_no):
        """
        Update the username and contact number for a given chat.

        :param chat_id: Chat session ID.
        :param username: The user's name.
        :param contact_no: The user's contact number.
        """
        chat_key = f"chat:{chat_id}"
        if not self.client.exists(chat_key):
            self.init_chat(chat_id)  # fallback init if it didn't exist

        self.client.hset(chat_key, mapping={
            "username": username,
            "contact_no": contact_no
        })

    def update_context(self, chat_id, key, value):
        """
        Update (or add) a specific context key for the given chat session.
        Commonly used for storing conversation context or other fields.

        :param chat_id: The chat session ID.
        :param key: The field name to update (e.g., "context").
        :param value: The new value to store.
        """
        chat_key = f"chat:{chat_id}"
        self.client.hset(chat_key, key, value)

    def get_context(self, chat_id, key):
        """
        Retrieve a specific context key for a given chat session.

        :param chat_id: The chat session ID.
        :param key: The field name (e.g., "context", "status").
        :return: The value stored, or None if not found.
        """
        chat_key = f"chat:{chat_id}"
        return self.client.hget(chat_key, key)

    def finish_chat(self, chat_id):
        """
        Mark the chat session as 'completed'. You can also use this 
        to run any end-of-chat operations, like archiving data.

        :param chat_id: The chat session ID to finish.
        """
        chat_key = f"chat:{chat_id}"
        if self.client.exists(chat_key):
            self.client.hset(chat_key, "status", "completed")
        # Optionally remove or archive the chat entirely,
        # e.g., self.client.delete(chat_key)
