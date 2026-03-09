import os
import random
import asyncio
import logging
from telethon import TelegramClient, functions, errors
from telethon.tl.types import Chat, Channel
from .proxy import get_rotating_proxy
import socks

logger = logging.getLogger(__name__)

def generate_device_info():
    devices = [
        {
            'device_model': 'iPhone 14 Pro',
            'system_version': 'iOS 16.5.1',
            'app_version': '9.6.1',
            'lang_code': 'ru',
            'system_lang_code': 'ru-RU'
        },
        {
            'device_model': 'Samsung Galaxy S23',
            'system_version': 'Android 13.0',
            'app_version': '9.6.1',
            'lang_code': 'ru',
            'system_lang_code': 'ru-RU'
        },
        {
            'device_model': 'Windows PC',
            'system_version': 'Windows 11',
            'app_version': '4.8.4',
            'lang_code': 'ru',
            'system_lang_code': 'ru-RU'
        },
        {
            'device_model': 'MacBook Pro',
            'system_version': 'macOS 13.4',
            'app_version': '9.6.1',
            'lang_code': 'ru',
            'system_lang_code': 'ru-RU'
        }
    ]
    device = random.choice(devices)
    device['app_version'] = f"{device['app_version']} ({random.randint(100, 999)})"
    return device

def create_client_with_proxy(session_path, proxy_config=None):
    device_info = generate_device_info()
    if proxy_config:
        # Map our proxy dict to Telethon's PySocks tuple
        proxy_type = proxy_config.get('proxy_type', 'socks5')
        if isinstance(proxy_type, str):
            proxy_type_lower = proxy_type.lower()
            if proxy_type_lower == 'socks5':
                proxy_type_val = socks.SOCKS5
            elif proxy_type_lower == 'socks4':
                proxy_type_val = socks.SOCKS4
            elif proxy_type_lower in ['http', 'https']:
                proxy_type_val = socks.HTTP
            else:
                proxy_type_val = socks.SOCKS5
        else:
            # Fallback if given a different enum; default to SOCKS5
            proxy_type_val = socks.SOCKS5

        proxy_tuple = (
            proxy_type_val,
            proxy_config.get('addr'),
            int(proxy_config.get('port', 0)),
            True,
            proxy_config.get('username'),
            proxy_config.get('password')
        )

        logger.info(f"Creating TelegramClient with proxy {proxy_tuple} for session {session_path}")
        client = TelegramClient(
            session_path,
            api_id=os.getenv('API_ID'),
            api_hash=os.getenv('API_HASH'),
            device_model=device_info['device_model'],
            system_version=device_info['system_version'],
            app_version=device_info['app_version'],
            lang_code=device_info['lang_code'],
            system_lang_code=device_info['system_lang_code'],
            proxy=proxy_tuple
        )
    else:
        logger.info(f"Creating TelegramClient without proxy for session {session_path}")
        client = TelegramClient(
            session_path,
            api_id=os.getenv('API_ID'),
            api_hash=os.getenv('API_HASH'),
            device_model=device_info['device_model'],
            system_version=device_info['system_version'],
            app_version=device_info['app_version'],
            lang_code=device_info['lang_code'],
            system_lang_code=device_info['system_lang_code']
        )
    return client

async def get_account_contacts(session_file):
    try:
        sessions_dir = os.path.abspath('sessions')
        session_path = os.path.join(sessions_dir, session_file)
        proxy_config = get_rotating_proxy()
        client = create_client_with_proxy(session_path, proxy_config)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return []
        try:
            contacts = await client(functions.contacts.GetContactsRequest(hash=0))
            contact_list = []
            if hasattr(contacts, 'users'):
                for contact in contacts.users:
                    if hasattr(contact, 'id') and hasattr(contact, 'first_name'):
                        contact_list.append({
                            'id': contact.id,
                            'first_name': contact.first_name,
                            'last_name': getattr(contact, 'last_name', ''),
                            'username': getattr(contact, 'username', ''),
                            'phone': getattr(contact, 'phone', '')
                        })
        except Exception as e:
            contact_list = []
        await client.disconnect()
        return contact_list
    except Exception as e:
        return []

async def get_account_dialogs(session_file):
    try:
        sessions_dir = os.path.abspath('sessions')
        session_path = os.path.join(sessions_dir, session_file)
        proxy_config = get_rotating_proxy()
        client = create_client_with_proxy(session_path, proxy_config)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return []
        try:
            dialogs = await client.get_dialogs()
            dialog_list = []
            for dialog in dialogs:
                if hasattr(dialog, 'entity') and dialog.entity:
                    entity = dialog.entity
                    if getattr(entity, 'megagroup', False) or getattr(entity, 'broadcast', False) or entity.__class__.__name__ in ['Channel', 'Chat']:
                        dialog_list.append({
                            'id': entity.id,
                            'title': getattr(entity, 'title', ''),
                            'username': getattr(entity, 'username', ''),
                            'type': 'channel' if getattr(entity, 'broadcast', False) else 'group'
                        })
        except Exception as e:
            dialog_list = []
        await client.disconnect()
        return dialog_list
    except Exception as e:
        return []

async def handle_spam_block(client, chat):
    try:
        await client.send_message('SpamBot', '/start')
        await asyncio.sleep(2)
        messages = await client.get_messages('SpamBot', limit=5)
        for msg in messages:
            text = msg.text.lower() if hasattr(msg, 'text') and msg.text else ""
            if 'blocked' in text or 'spam' in text or 'restrict' in text:
                if 'temporarily limited' in text:
                    import re
                    time_match = re.search(r'(\d+) hours?', text)
                    if time_match:
                        return False
                await client.send_message('SpamBot', '/unblock')
                follow_up = await client.get_messages('SpamBot', limit=2)
                for response in follow_up:
                    resp_text = response.text.lower() if hasattr(response, 'text') and response.text else ""
                    if 'good news' in resp_text or 'successful' in resp_text or 'no longer' in resp_text:
                        return True
        return False
    except Exception as e:
        return False


async def search_telegram_groups(client, keywords):
    from .session import get_session_files
    from .proxy import parse_proxy

    found_groups = set()
    invalid_sessions = []
    total_keywords = len(keywords)

    session_files = get_session_files()
    if not session_files:
        return [], []

    proxies = get_rotating_proxy()
    working_pairs = []
    if proxies:
        for session_file in session_files:
            for proxy in proxies:
                working_pairs.append((session_file, proxy))
    else:
        for session_file in session_files:
            working_pairs.append((session_file, None))

    current_session_index = 0
    current_session = session_files[current_session_index]

    search_terms = []
    for keyword in keywords:
        search_terms.append(f"{keyword} чат")

    async def switch_session():
        nonlocal client, current_session_index
        try:
            if client and client.is_connected():
                await client.disconnect()
            current_session_index = (current_session_index + 1) % len(working_pairs)
            session_file, proxy = working_pairs[current_session_index]
            if proxy:
                proxy_dict = parse_proxy(proxy)
            else:
                proxy_dict = None
            client = create_client_with_proxy(session_file, proxy_dict)
            await client.connect()
            if not await client.is_user_authorized():
                return False
            return True
        except Exception as e:
            return False

    async def ensure_client_connected():
        if not client.is_connected():
            await client.connect()
            if not await client.is_user_authorized():
                invalid_sessions.append(current_session)
                return await switch_session()
        return True

    for i, search_term in enumerate(search_terms, 1):
        try:
            if not await ensure_client_connected():
                return list(found_groups), invalid_sessions

            result = await client(functions.messages.SearchRequest(
                peer='@asdasdasdasd',
                q=search_term,
                filter=functions.channels.GetFullChannelRequest(channel=functions.InputChannelEmpty()),
                min_date=None,
                max_date=None,
                offset_id=0,
                add_offset=0,
                limit=100,
                max_id=0,
                min_id=0,
                hash=0
            ))

            for chat in result.chats:
                try:
                    if not await ensure_client_connected():
                        return list(found_groups), invalid_sessions
                    if not hasattr(chat, 'username') or not chat.username:
                        continue
                    username = chat.username
                    found_groups.add(username)
                except Exception as e:
                    continue
            await asyncio.sleep(2)
        except errors.FloodWaitError as e:
            if not await switch_session():
                return list(found_groups), invalid_sessions
        except (errors.SessionPasswordNeededError, errors.UnauthorizedError,
                errors.AuthKeyUnregisteredError, errors.SessionRevokedError,
                errors.UserDeactivatedError, errors.PhoneNumberBannedError,
                errors.PhoneCodeInvalidError, errors.PhoneCodeExpiredError) as e:
            invalid_sessions.append(getattr(client, 'session', 'unknown'))
            if not await switch_session():
                return list(found_groups), invalid_sessions
        except Exception as e:
            continue

    return list(found_groups), invalid_sessions
