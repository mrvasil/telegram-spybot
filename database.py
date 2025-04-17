import sqlite3
import os
from datetime import datetime, timedelta
from aiogram import types

def get_extension_from_mime(mime_type: str) -> str:
    mime_to_ext = {
        'image/jpeg': '.jpg',
        'image/png': '.png',
        'image/gif': '.gif',
        'video/mp4': '.mp4',
        'audio/mpeg': '.mp3',
        'audio/ogg': '.ogg',
        'application/pdf': '.pdf'
    }
    return mime_to_ext.get(mime_type, '')
    
def get_file_extension(file_name: str) -> str:
    if not file_name:
        return ''
    parts = file_name.rsplit('.', 1)
    if len(parts) > 1:
        return f".{parts[1].lower()}"
    return ''

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

        cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value INTEGER
        )
        ''')

        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('notify_edited', 1)")
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('notify_deleted', 1)")
        
        conn.commit()
        conn.close()
        
    async def save_message(self, message: types.Message):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        media_files = []
        
        if message.photo:
            if message.media_group_id:
                largest_photo = message.photo[-1]
                media_files.append(("photo", largest_photo.file_id, message.media_group_id, '.jpg'))
            else:
                largest_photo = message.photo[-1]
                media_files.append(("photo", largest_photo.file_id, 0, '.jpg'))
        if message.video:
            ext = get_extension_from_mime(message.video.mime_type) or get_file_extension(message.video.file_name) or '.mp4'
            media_files.append(("video", message.video.file_id, 0, ext))
        if message.video_note:
            media_files.append(("video_note", message.video_note.file_id, 0, '.mp4'))
        if message.voice:
            ext = get_extension_from_mime(message.voice.mime_type) or '.ogg'
            media_files.append(("voice", message.voice.file_id, 0, ext))
        if message.audio:
            ext = get_extension_from_mime(message.audio.mime_type) or get_file_extension(message.audio.file_name) or '.mp3'
            media_files.append(("audio", message.audio.file_id, 0, ext))
        if message.animation:
            ext = get_extension_from_mime(message.animation.mime_type) or '.gif'
            media_files.append(("animation", message.animation.file_id, 0, ext))
        if message.document and not message.animation:
            if message.document.mime_type == 'image/gif':
                media_files.append(("animation", message.document.file_id, 0, '.gif'))
            else:
                ext = get_extension_from_mime(message.document.mime_type) or get_file_extension(message.document.file_name) or ''
                media_files.append(("document", message.document.file_id, 0, ext))
        if message.sticker:
            media_files.append(("sticker", message.sticker.file_id, 0, '.webp'))
            
        created_at = datetime.now().isoformat()
        
        cursor.execute(
            "INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message.chat.id,
                message.message_id,
                message.from_user.id if message.from_user else None,
                message.from_user.username if message.from_user else None,
                message.caption or message.text or ("*стикер*" if message.sticker else "*кружок*" if message.video_note else ""),
                message.date.isoformat(),
                created_at
            )
        )
        
        saved_media = []
        for media_type, file_id, group_id, extension in media_files:
            if not os.path.exists("media"):
                os.makedirs("media")
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            media_path = f"media/{timestamp}_{media_type}_{group_id}{extension}"
            
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
            "SELECT media_type, media_path, file_id FROM media_files WHERE chat_id = ? AND message_id = ?",
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

    def get_stats(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM messages")
        total_messages = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM media_files")
        total_media = cursor.fetchone()[0]
        
        cursor.execute("SELECT created_at FROM messages ORDER BY created_at DESC LIMIT 1")
        last_message = cursor.fetchone()
        last_message_time = last_message[0] if last_message else None
        
        conn.close()
        
        return {
            "total_messages": total_messages,
            "total_media": total_media,
            "last_message_time": last_message_time
        }

    def get_settings(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT key, value FROM settings")
        settings = dict(cursor.fetchall())
        
        conn.close()
        
        return {
            "notify_edited": bool(settings.get("notify_edited", 1)),
            "notify_deleted": bool(settings.get("notify_deleted", 1))
        }

    def toggle_setting(self, key: str):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("UPDATE settings SET value = 1 - value WHERE key = ?", (key,))
        
        conn.commit()
        conn.close()