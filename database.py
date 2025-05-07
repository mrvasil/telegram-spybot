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
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE,
            first_seen TEXT
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            chat_id INTEGER,
            message_id INTEGER,
            user_id INTEGER,
            text TEXT,
            date TEXT,
            is_forwarded INTEGER DEFAULT 0,
            forward_from TEXT,
            PRIMARY KEY (chat_id, message_id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        ''')
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS message_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            message_id INTEGER,
            action_type TEXT,
            old_text TEXT,
            new_text TEXT,
            action_date TEXT,
            FOREIGN KEY (chat_id, message_id) REFERENCES messages(chat_id, message_id)
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
            FOREIGN KEY (chat_id, message_id) REFERENCES messages(chat_id, message_id)
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
        
        cursor.execute(
            "INSERT OR REPLACE INTO users (id, username, first_seen) VALUES (?, ?, ?)",
            (
                message.from_user.id,
                message.from_user.username,
                datetime.now().isoformat()
            )
        )
        
        is_forwarded = 0
        forward_from = ""
        
        if message.forward_from:
            is_forwarded = 1
            if message.forward_from.username:
                forward_from = f"@{message.forward_from.username}"
            else:
                forward_from = message.forward_from.full_name
        elif message.forward_from_chat:
            is_forwarded = 1
            if message.forward_from_chat.username:
                forward_from = f"@{message.forward_from_chat.username}"
            else:
                forward_from = message.forward_from_chat.title
        elif message.forward_sender_name:
            is_forwarded = 1
            forward_from = message.forward_sender_name
        
        text = message.md_text or message.caption or " "
        
        cursor.execute(
            "INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message.chat.id,
                message.message_id,
                message.from_user.id,
                text,
                message.date.isoformat(),
                is_forwarded,
                forward_from
            )
        )
        
        media_files = []
        if message.photo:
            largest_photo = message.photo[-1]
            media_files.append(("photo", largest_photo.file_id, '.jpg'))
        if message.video:
            ext = get_extension_from_mime(message.video.mime_type) or get_file_extension(message.video.file_name) or '.mp4'
            media_files.append(("video", message.video.file_id, ext))
        if message.video_note:
            media_files.append(("video_note", message.video_note.file_id, '.mp4'))
        if message.voice:
            ext = get_extension_from_mime(message.voice.mime_type) or '.ogg'
            media_files.append(("voice", message.voice.file_id, ext))
        if message.audio:
            ext = get_extension_from_mime(message.audio.mime_type) or get_file_extension(message.audio.file_name) or '.mp3'
            media_files.append(("audio", message.audio.file_id, ext))
        if message.animation:
            ext = get_extension_from_mime(message.animation.mime_type) or '.gif'
            media_files.append(("animation", message.animation.file_id, ext))
        if message.document and not message.animation:
            if message.document.mime_type == 'image/gif':
                media_files.append(("animation", message.document.file_id, '.gif'))
            else:
                ext = get_extension_from_mime(message.document.mime_type) or get_file_extension(message.document.file_name) or ''
                media_files.append(("document", message.document.file_id, ext))
        if message.sticker:
            media_files.append(("sticker", message.sticker.file_id, '.webp'))
            
        saved_media = []
        for media_type, file_id, extension in media_files:
            if not os.path.exists("media"):
                os.makedirs("media")
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            media_path = f"media/{timestamp}_{media_type}{extension}"
            
            cursor.execute(
                "INSERT INTO media_files (chat_id, message_id, file_id, media_type, media_path) VALUES (?, ?, ?, ?, ?)",
                (message.chat.id, message.message_id, file_id, media_type, media_path)
            )
            saved_media.append((media_path, file_id))
        
        conn.commit()
        conn.close()
        return saved_media

    def save_message_action(self, chat_id: int, message_id: int, action_type: str, old_text: str, new_text: str = None):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if action_type == 'edit':
            cursor.execute(
                "UPDATE messages SET text = ? WHERE chat_id = ? AND message_id = ?",
                (new_text, chat_id, message_id)
            )
        
        cursor.execute(
            "INSERT INTO message_actions (chat_id, message_id, action_type, old_text, new_text, action_date) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, message_id, action_type, old_text, new_text, datetime.now().isoformat())
        )
        
        conn.commit()
        conn.close()

    def get_message(self, chat_id: int, message_id: int):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT m.*, u.username 
            FROM messages m
            JOIN users u ON m.user_id = u.id
            WHERE m.chat_id = ? AND m.message_id = ?
        """, (chat_id, message_id))
        
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
            "text": row[3],
            "date": row[4],
            "is_forwarded": bool(row[5]),
            "forward_from": row[6],
            "username": row[7],
            "media_files": media_files
        }

    def get_user_actions(self, username: str, limit: int = 5):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
        user_row = cursor.fetchone()
        if not user_row:
            conn.close()
            return None, []
            
        user_id = user_row[0]
        
        cursor.execute("""
            SELECT 
                ma.action_type,
                ma.old_text,
                ma.new_text,
                ma.action_date,
                m.is_forwarded,
                m.forward_from,
                m.chat_id,
                m.message_id
            FROM message_actions ma
            JOIN messages m ON ma.chat_id = m.chat_id AND ma.message_id = m.message_id
            WHERE m.user_id = ?
            ORDER BY ma.action_date DESC
            LIMIT ?
        """, (user_id, limit))
        
        actions = []
        for row in cursor.fetchall():
            action_type = row[0]
            old_text = row[1]
            new_text = row[2]
            action_date = row[3]
            is_forwarded = bool(row[4])
            forward_from = row[5]
            chat_id = row[6]
            message_id = row[7]
            
            display_text = old_text if action_type == 'delete' else new_text
            action_name = 'deleted' if action_type == 'delete' else 'edited'
            
            actions.append((action_name, display_text, action_date, is_forwarded, forward_from, chat_id, message_id))
        
        conn.close()
        return user_id, actions

    def delete_message(self, chat_id: int, message_id: int):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT text FROM messages WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
        row = cursor.fetchone()
        if row:
            self.save_message_action(chat_id, message_id, 'delete', row[0])
        
        cursor.execute(
            "SELECT media_path FROM media_files WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id)
        )
        
        media_paths = [row[0] for row in cursor.fetchall()]
        
        for media_path in media_paths:
            if media_path and os.path.exists(media_path):
                os.remove(media_path)
        
        conn.commit()
        conn.close()
        return media_paths

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

    def get_stats(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM messages")
        total_messages = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM media_files")
        total_media = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            "total_messages": total_messages,
            "total_media": total_media
        }

    def cleanup_old_messages(self, hours=24):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cutoff_time = (datetime.now() - timedelta(hours=hours)).isoformat()
        
        cursor.execute("SELECT chat_id, message_id FROM messages WHERE date < ?", (cutoff_time,))
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

    def cleanup_all(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM messages")
        messages_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT media_path FROM media_files")
        media_paths = [row[0] for row in cursor.fetchall()]
        files_count = len(media_paths)
        
        for media_path in media_paths:
            if media_path and os.path.exists(media_path):
                try:
                    os.remove(media_path)
                except:
                    pass
                    
        if os.path.exists("media"):
            try:
                os.rmdir("media")
            except:
                pass
        
        cursor.execute("DELETE FROM message_actions")
        cursor.execute("DELETE FROM media_files")
        cursor.execute("DELETE FROM messages")
        cursor.execute("DELETE FROM users")
        
        conn.commit()
        conn.close()
        
        return messages_count, files_count

    def get_user_stats(self, username: str):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                u.id,
                COUNT(DISTINCT m.message_id) as total_messages,
                COUNT(DISTINCT ma.id) as total_actions,
                COUNT(DISTINCT mf.id) as total_media
            FROM users u
            LEFT JOIN messages m ON u.id = m.user_id
            LEFT JOIN message_actions ma ON m.chat_id = ma.chat_id AND m.message_id = ma.message_id
            LEFT JOIN media_files mf ON m.chat_id = mf.chat_id AND m.message_id = mf.message_id
            WHERE u.username = ?
            GROUP BY u.id
        """, (username,))
        
        row = cursor.fetchone()
        if not row:
            conn.close()
            return None
            
        user_id = row[0]
        
        cursor.execute("""
            SELECT value FROM settings 
            WHERE key = ? 
        """, (f"notify_user_{user_id}",))
        
        notify_row = cursor.fetchone()
        notify_enabled = bool(notify_row[0]) if notify_row else True
        
        conn.close()
        
        return {
            "user_id": user_id,
            "total_messages": row[1],
            "total_actions": row[2],
            "total_media": row[3],
            "notify_enabled": notify_enabled
        }

    def toggle_user_notify(self, user_id: int):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        key = f"notify_user_{user_id}"
        
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        
        if row is None:
            cursor.execute("INSERT INTO settings (key, value) VALUES (?, 0)", (key,))
        else:
            cursor.execute("UPDATE settings SET value = 1 - value WHERE key = ?", (key,))
        
        conn.commit()
        conn.close()

    def cleanup_user_data(self, username: str):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
        user_row = cursor.fetchone()
        if not user_row:
            conn.close()
            return 0, 0
            
        user_id = user_row[0]
        
        cursor.execute("""
            SELECT chat_id, message_id FROM messages 
            WHERE user_id = ?
        """, (user_id,))
        
        messages = cursor.fetchall()
        deleted_messages = 0
        deleted_files = 0
        
        for chat_id, message_id in messages:
            cursor.execute(
                "SELECT media_path FROM media_files WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id)
            )
            
            media_paths = [row[0] for row in cursor.fetchall()]
            
            for media_path in media_paths:
                if media_path and os.path.exists(media_path):
                    try:
                        os.remove(media_path)
                        deleted_files += 1
                    except:
                        pass
                        
            cursor.execute("DELETE FROM media_files WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
            cursor.execute("DELETE FROM message_actions WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
            cursor.execute("DELETE FROM messages WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
            deleted_messages += 1
        
        conn.commit()
        conn.close()
        
        return deleted_messages, deleted_files

    def get_message_history(self, message_id: int):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT 
                COALESCE(
                    (SELECT old_text 
                     FROM message_actions 
                     WHERE chat_id = m.chat_id 
                     AND message_id = m.message_id 
                     AND action_type = 'edit'
                     ORDER BY action_date ASC
                     LIMIT 1),
                    m.text
                ) as original_text,
                m.chat_id,
                m.date,
                m.is_forwarded,
                m.forward_from,
                u.username,
                m.text as current_text
            FROM messages m
            JOIN users u ON m.user_id = u.id
            WHERE m.message_id = ?
        """, (message_id,))
        message_info = cursor.fetchone()
        
        if not message_info:
            conn.close()
            return None
            
        original_text, chat_id, date, is_forwarded, forward_from, username, current_text = message_info
        
        cursor.execute("""
            SELECT action_type, old_text, new_text, action_date
            FROM message_actions
            WHERE message_id = ? AND chat_id = ?
            ORDER BY action_date ASC
        """, (message_id, chat_id))
        actions = cursor.fetchall()
        
        cursor.execute("""
            SELECT media_type, media_path, file_id
            FROM media_files
            WHERE chat_id = ? AND message_id = ?
        """, (chat_id, message_id))
        media_files = cursor.fetchall()
        
        conn.close()
        
        return {
            'original_text': original_text,
            'chat_id': chat_id,
            'date': date,
            'is_forwarded': is_forwarded,
            'forward_from': forward_from,
            'username': username,
            'actions': actions,
            'media_files': media_files
        }