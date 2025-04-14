import sqlite3
import os
from datetime import datetime
from aiogram import types

class Database:
    def __init__(self, db_path="messages.db"):
        self.db_path = db_path
        self._create_tables()
        
    def _create_tables(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            chat_id INTEGER,
            message_id INTEGER,
            user_id INTEGER,
            username TEXT,
            text TEXT,
            date TEXT,
            media_path TEXT,
            PRIMARY KEY (chat_id, message_id)
        )
        ''')
        
        conn.commit()
        conn.close()
        
    async def save_message(self, message: types.Message):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        media_path = None
        file_id = None

        if message.photo:
            file_id = message.photo[-1].file_id
            media_type = "photo"
        elif message.video:
            file_id = message.video.file_id
            media_type = "video"
        elif message.voice:
            file_id = message.voice.file_id
            media_type = "voice"
        elif message.audio:
            file_id = message.audio.file_id
            media_type = "audio"
        elif message.document:
            file_id = message.document.file_id
            media_type = "document"
        else:
            file_id = None
            media_type = None
            
        if file_id:
            if not os.path.exists("media"):
                os.makedirs("media")
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            media_path = f"media/{timestamp}_{media_type}"
        
        cursor.execute(
            "INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message.chat.id,
                message.message_id,
                message.from_user.id if message.from_user else None,
                message.from_user.username if message.from_user else None,
                message.text or message.caption or "",
                message.date.isoformat(),
                media_path
            )
        )
        
        conn.commit()
        conn.close()
        
        return media_path, file_id
        
    def get_message(self, chat_id, message_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        )
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return None
            
        return {
            "chat_id": row[0],
            "message_id": row[1],
            "user_id": row[2],
            "username": row[3],
            "text": row[4],
            "date": row[5],
            "media_path": row[6]
        }
        
    def delete_message(self, chat_id, message_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT media_path FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        )
        
        row = cursor.fetchone()
        media_path = row[0] if row else None

        cursor.execute(
            "DELETE FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        )
        
        conn.commit()
        conn.close()
        
        if media_path and os.path.exists(media_path):
            os.remove(media_path)
        
        return media_path