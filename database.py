import sqlite3
import os
from datetime import datetime, timedelta
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
            created_at TEXT,
            PRIMARY KEY (chat_id, message_id)
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS media_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            message_id INTEGER,
            file_id TEXT,
            media_type TEXT,
            media_path TEXT,
            FOREIGN KEY (chat_id, message_id) REFERENCES messages (chat_id, message_id) ON DELETE CASCADE
        )
        ''')
        
        conn.commit()
        conn.close()
        
    async def save_message(self, message: types.Message):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        media_files = []
        
        if message.photo:
            if message.media_group_id:
                largest_photo = message.photo[-1]
                media_files.append(("photo", largest_photo.file_id, message.media_group_id))
            else:
                largest_photo = message.photo[-1]
                media_files.append(("photo", largest_photo.file_id, 0))
        if message.video:
            media_files.append(("video", message.video.file_id, 0))
        if message.voice:
            media_files.append(("voice", message.voice.file_id, 0))
        if message.audio:
            media_files.append(("audio", message.audio.file_id, 0))
        if message.document:
            media_files.append(("document", message.document.file_id, 0))
            
        created_at = datetime.now().isoformat()
        
        cursor.execute(
            "INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message.chat.id,
                message.message_id,
                message.from_user.id if message.from_user else None,
                message.from_user.username if message.from_user else None,
                message.text or message.caption or "",
                message.date.isoformat(),
                created_at
            )
        )
        
        saved_media = []
        for media_type, file_id, group_id in media_files:
            if not os.path.exists("media"):
                os.makedirs("media")
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            media_path = f"media/{timestamp}_{media_type}_{group_id}"
            
            cursor.execute(
                "INSERT INTO media_files (chat_id, message_id, file_id, media_type, media_path) VALUES (?, ?, ?, ?, ?)",
                (message.chat.id, message.message_id, file_id, media_type, media_path)
            )
            saved_media.append((media_path, file_id))
        
        conn.commit()
        conn.close()
        
        return saved_media
        
    def get_message(self, chat_id, message_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        )
        
        row = cursor.fetchone()
        if not row:
            conn.close()
            return None
            
        cursor.execute(
            "SELECT media_type, media_path FROM media_files WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        )
        
        media_files = cursor.fetchall()
        conn.close()
            
        return {
            "chat_id": row[0],
            "message_id": row[1],
            "user_id": row[2],
            "username": row[3],
            "text": row[4],
            "date": row[5],
            "created_at": row[6],
            "media_files": media_files
        }
        
    def delete_message(self, chat_id, message_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT media_path FROM media_files WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        )
        
        media_paths = [row[0] for row in cursor.fetchall()]

        cursor.execute(
            "DELETE FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        )
        
        conn.commit()
        conn.close()
        
        for media_path in media_paths:
            if media_path and os.path.exists(media_path):
                os.remove(media_path)
        
        return media_paths
        
    def cleanup_old_messages(self, hours=24):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cutoff_time = (datetime.now() - timedelta(hours=hours)).isoformat()
        
        cursor.execute("SELECT chat_id, message_id FROM messages WHERE created_at < ?", (cutoff_time,))
        old_messages = cursor.fetchall()
        
        deleted_count = 0
        for chat_id, message_id in old_messages:
            cursor.execute("SELECT media_path FROM media_files WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
            media_paths = [row[0] for row in cursor.fetchall()]
            
            for media_path in media_paths:
                if media_path and os.path.exists(media_path):
                    try:
                        os.remove(media_path)
                    except:
                        pass
            
            cursor.execute("DELETE FROM messages WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
            deleted_count += 1
        
        conn.commit()
        conn.close()
        
        return deleted_count