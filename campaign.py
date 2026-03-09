import os
import json
import asyncio
import random
import string
import time
import logging
from datetime import datetime
from telethon import errors, functions

from utils.session import get_session_files, validate_session, delete_invalid_session, get_async_session_lock
from utils.proxy import get_rotating_proxy, parse_proxy
from utils.telegram import create_client_with_proxy, get_account_contacts, get_account_dialogs
from utils.message import get_random_message_variant
from utils.database import save_campaign_to_db
import config

logger = logging.getLogger(__name__)

active_campaigns = {}
completed_campaigns = []
active_invoices = {}

def load_completed_campaigns():
    try:
        with open('campaigns.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def save_completed_campaigns():
    with open('campaigns.json', 'w') as f:
        json.dump(completed_campaigns, f)

async def create_invoice(amount, user_id):
    try:
        fake_invoice = {
            'invoice_id': ''.join(random.choices(string.ascii_letters + string.digits, k=10)),
            'status': 'active',
            'pay_url': '#',
            'amount': '0',
            'asset': 'USDT'
        }
        active_invoices[fake_invoice['invoice_id']] = {
            'user_id': user_id,
            'amount': 0,
            'status': 'pending'
        }
        return fake_invoice
    except Exception as e:
        return None

async def check_invoice(invoice_id):
    return True

async def send_message_to_chat(client, chat_id, message_data, campaign, semaphore, session_file=None, proxy_config=None):
    try:
        campaign['total_attempts'] += 1
        message_type = message_data.get('type', 'text')
        message_text = message_data.get('text', '')
        media_path = message_data.get('media_path', '')
        interval = max(1, int(campaign.get('interval', 1)))

        # Разрешаем цель (юзер/чат) в сущность
        target = chat_id
        try:
            # Поддержка @username, https://t.me/..., -100..., чисел и Title
            # Telethon сам разберёт строки и ссылки, для int нужен, чтобы был в диалогах/кэше
            target = await client.get_entity(chat_id)
        except Exception as e:
            # Попробуем альтернативы для ссылок вида https://t.me/xxx
            try:
                if isinstance(chat_id, str) and chat_id.startswith('https://t.me/'):
                    alias = chat_id.replace('https://t.me/', '').strip().split('?')[0]
                    target = await client.get_entity(alias)
                else:
                    raise
            except Exception as ee:
                campaign['messages_failed'] += 1
                not_found = campaign.setdefault('unresolved_targets', [])
                not_found.append(str(chat_id))
                logger.exception(
                    "Failed to resolve target",
                    extra={
                        'chat_id': chat_id,
                        'session_file': session_file,
                        'error': str(ee)
                    }
                )
                await asyncio.sleep(interval)
                return False

        # Если это группа (@username), проверяем членство и вступаем если нужно
        if isinstance(chat_id, str) and chat_id.startswith('@'):
            try:
                # Проверяем, состоит ли уже в группе
                participant = await client(functions.channels.GetParticipantRequest(
                    channel=target,
                    participant=await client.get_me()
                ))
                logger.debug(f"Already member of {chat_id}")
            except errors.UserNotParticipantError:
                # Не состоит в группе, пытаемся вступить
                try:
                    logger.info(f"Attempting to join group {chat_id} before sending message")
                    await client(functions.channels.JoinChannelRequest(channel=target))
                    logger.info(f"Successfully joined {chat_id}")
                    # Пауза после вступления
                    await asyncio.sleep(random.uniform(2, 4))
                except errors.FloodWaitError as e:
                    logger.warning(f"FloodWaitError joining {chat_id}, waiting {e.seconds} seconds")
                    await asyncio.sleep(e.seconds)
                    try:
                        await client(functions.channels.JoinChannelRequest(channel=target))
                        logger.info(f"Successfully joined {chat_id} after flood wait")
                    except Exception as retry_e:
                        logger.warning(f"Failed to join {chat_id} after retry: {retry_e}")
                        campaign['messages_failed'] += 1
                        campaign.setdefault('failed_joins', []).append(str(chat_id))
                        await asyncio.sleep(interval)
                        return False
                except (errors.ChatWriteForbiddenError, errors.UserBannedInChannelError, 
                        errors.ChannelsTooMuchError, errors.InviteHashExpiredError) as e:
                    logger.warning(f"Cannot join {chat_id}: {e}")
                    campaign['messages_failed'] += 1
                    campaign.setdefault('failed_joins', []).append(str(chat_id))
                    await asyncio.sleep(interval)
                    return False
                except Exception as e:
                    logger.warning(f"Failed to join {chat_id}: {e}")
                    campaign['messages_failed'] += 1
                    campaign.setdefault('failed_joins', []).append(str(chat_id))
                    await asyncio.sleep(interval)
                    return False
            except Exception as e:
                logger.warning(f"Could not check membership for {chat_id}: {e}")
                # Продолжаем попытку отправки, возможно группа доступна

        # Применяем рандомизацию текста для всех типов сообщений (включая caption для медиа)
        if message_text:
            randomized_text = get_random_message_variant(message_text)
            message_text = randomized_text

        if message_type == 'text':
            await client.send_message(target, message_text)
        elif message_type == 'photo':
            await client.send_file(target, media_path, caption=message_text)
        elif message_type == 'video':
            await client.send_file(target, media_path, caption=message_text)
        elif message_type == 'document':
            await client.send_file(target, media_path, caption=message_text)
        elif message_type == 'audio':
            await client.send_file(target, media_path, caption=message_text)
        elif message_type == 'voice':
            await client.send_file(target, media_path, voice_note=True)
        elif message_type == 'sticker':
            await client.send_file(target, message_data.get('sticker_file_id'))
        elif message_type == 'animation':
            await client.send_file(target, media_path, caption=message_text)
        elif message_type == 'poll':
            poll_data = message_data.get('poll', {})
            await client.send_message(target, poll_data['question'])
        elif message_type == 'contact':
            contact_data = message_data.get('contact', {})
            await client.send_contact(target, contact_data['phone_number'],
                                    contact_data['first_name'], contact_data['last_name'])
        elif message_type == 'location':
            location_data = message_data.get('location', {})
            await client.send_location(target, location_data['latitude'], location_data['longitude'])
        elif message_type == 'venue':
            venue_data = message_data.get('venue', {})
            await client.send_location(target, venue_data['latitude'], venue_data['longitude'])

        campaign['messages_sent'] += 1
        # Реальный интервал между сообщениями
        await asyncio.sleep(interval)
        return True
    except Exception as e:
        campaign['messages_failed'] += 1
        logger.exception(
            "Failed to send message",
            extra={
                'chat_id': chat_id,
                'message_type': message_data.get('type'),
                'session_file': session_file,
                'proxy': proxy_config,
                'error': str(e)
            }
        )
        return False

async def send_message_with_client(client, chat_id, message_data, campaign, semaphore, session_file=None, proxy_config=None):
    async with semaphore:
        try:
            if session_file:
                session_lock = get_async_session_lock(session_file)
                async with session_lock:
                    logger.debug(f"Connecting client for session {session_file} | chat {chat_id} | proxy={proxy_config}")
                    await client.connect()
                    if not await client.is_user_authorized():
                        # Отметим сессию как невалидную для итогового отчёта
                        try:
                            invalid = campaign.setdefault('invalid_sessions', set())
                            invalid.add(session_file)
                        except Exception:
                            pass
                        await client.disconnect()
                        return False
                    success = await send_message_to_chat(
                        client, chat_id, message_data, campaign, semaphore,
                        session_file=session_file, proxy_config=proxy_config
                    )
                    await client.disconnect()
                    return success
            else:
                logger.debug(f"Connecting client (no session_file) | chat {chat_id} | proxy={proxy_config}")
                await client.connect()
                if not await client.is_user_authorized():
                    try:
                        invalid = campaign.setdefault('invalid_sessions', set())
                        invalid.add(session_file or 'unknown')
                    except Exception:
                        pass
                    await client.disconnect()
                    return False
                success = await send_message_to_chat(
                    client, chat_id, message_data, campaign, semaphore,
                    session_file=session_file, proxy_config=proxy_config
                )
                await client.disconnect()
                return success
        except Exception as e:
            logger.exception(
                "Client error during connect/send",
                extra={
                    'chat_id': chat_id,
                    'session_file': session_file,
                    'proxy': proxy_config,
                    'error': str(e)
                }
            )
            try:
                await client.disconnect()
            except:
                pass
            return False

async def run_multithreaded_campaign(campaign, bot, max_workers=3, show_summary=True):
    try:
        from utils.proxy import load_proxies
        session_files = get_session_files()
        proxies = load_proxies()

        if not session_files:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Вернуться в меню", callback_data='back_to_main')]])
            await bot.send_message(chat_id=campaign['user_id'], text="❌ Нет доступных сессий для рассылки", reply_markup=kb)
            return

        # Делаем отправки последовательными, чтобы интервал между сообщениями имел эффект
        session_semaphore = asyncio.Semaphore(1)

        message_data = {
            'type': campaign.get('message_type', 'text'),
            'text': campaign.get('message', ''),
            'media_path': campaign.get('media_path', ''),
            'sticker_file_id': campaign.get('sticker_file_id', ''),
            'poll': campaign.get('poll', {}),
            'contact': campaign.get('contact', {}),
            'location': campaign.get('location', {}),
            'venue': campaign.get('venue', {})
        }

        campaign['messages_sent'] = 0
        campaign['messages_failed'] = 0
        campaign['total_attempts'] = 0

        targets_by_session = campaign.get('targets_by_session') or {}
        chats = campaign.get('chats', [])
        if not chats and not targets_by_session:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Вернуться в меню", callback_data='back_to_main')]])
            await bot.send_message(chat_id=campaign['user_id'], text="❌ Нет чатов для рассылки", reply_markup=kb)
            return

        results = []
        if targets_by_session:
            # Ретраи: если не вышло с конкретной сессией, пробуем другие
            all_session_files = list(targets_by_session.keys()) or session_files
            for session_file, session_targets in targets_by_session.items():
                for chat_id in session_targets:
                    sent = False
                    tried = 0
                    for candidate_session in [session_file] + [s for s in all_session_files if s != session_file]:
                        tried += 1
                        try:
                            proxy_config = None
                            if proxies:
                                proxy_config = get_rotating_proxy()
                            session_path = f'sessions/{candidate_session}'
                            client = create_client_with_proxy(session_path, proxy_config)
                            ok = await send_message_with_client(
                                client, chat_id, message_data, campaign, session_semaphore,
                                candidate_session, proxy_config
                            )
                            if ok:
                                results.append(True)
                                sent = True
                                break
                        except Exception as e:
                            logger.exception(
                                "Failed to send message",
                                extra={
                                    'chat_id': chat_id,
                                    'session_file': candidate_session,
                                    'proxy': proxy_config,
                                    'error': str(e)
                                }
                            )
                            continue
                    if not sent:
                        campaign['messages_failed'] += 1
                        results.append(False)
        else:
            # Последовательные отправки по списку чатов, случайная сессия на чат
            for i, chat_id in enumerate(chats):
                sent = False
                for session_file in session_files:
                    try:
                        proxy_config = None
                        if proxies:
                            proxy_config = get_rotating_proxy()
                        session_path = f'sessions/{session_file}'
                        client = create_client_with_proxy(session_path, proxy_config)
                        ok = await send_message_with_client(
                            client, chat_id, message_data, campaign, session_semaphore,
                            session_file, proxy_config
                        )
                        if ok:
                            results.append(True)
                            sent = True
                            break
                    except Exception as e:
                        logger.exception(
                            "Failed to send message",
                            extra={
                                'chat_id': chat_id,
                                'session_file': session_file,
                                'proxy': proxy_config,
                                'error': str(e)
                            }
                        )
                        continue
                if not sent:
                    campaign['messages_failed'] += 1
                    results.append(False)
        successful = sum(1 for result in results if result is True)
        failed = len(results) - successful

        campaign['status'] = 'completed'
        campaign['end_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        campaign['messages_sent'] = successful
        campaign['messages_failed'] = failed
        campaign['total_attempts'] = len(results)

        save_campaign_to_db(campaign)

        if show_summary:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Вернуться в меню", callback_data='back_to_main')]])

            invalid_sessions = campaign.get('invalid_sessions') or set()
            failed_joins = campaign.get('failed_joins') or []
            extra_hint = ""
            if successful == 0:
                extra_hint = "\n\nℹ️ Все отправки не удались. Проверьте валидность сессий и корректность идентификаторов чатов (@username, -100id или ссылка)."
            if invalid_sessions:
                extra_hint += f"\nНевалидных сессий: {len(invalid_sessions)}"
            unresolved = campaign.get('unresolved_targets') or []
            if unresolved:
                extra_hint += f"\nНеразрешённых целей: {len(unresolved)}"
            if failed_joins:
                extra_hint += f"\nНе удалось вступить в группы: {len(failed_joins)}"

            await bot.send_message(
                chat_id=campaign['user_id'],
                text=(
                    f"✅ Рассылка завершена!\n"
                    f"• Успешно отправлено: {successful}\n"
                    f"• Ошибок: {failed}\n"
                    f"• Всего попыток: {len(results)}\n"
                    f"• Процент успеха: {(successful/len(results)*100):.1f}%" + extra_hint
                ),
                reply_markup=kb
            )
    except Exception as e:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Вернуться в меню", callback_data='back_to_main')]])
        if show_summary:
            await bot.send_message(
                chat_id=campaign['user_id'],
                text=f"❌ Ошибка в многопоточной рассылке: {str(e)}",
                reply_markup=kb
            )

async def start_campaign(update, context):
    try:
        user = update.effective_user if hasattr(update, 'effective_user') else update.callback_query.from_user
        spam_type = context.user_data.get('spam_type', 'chats')

        if spam_type in ['contacts', 'joined']:
            if not context.user_data.get('message'):
                await update.message.reply_text("❌ Сообщение не найдено. Пожалуйста, введите сообщение для рассылки:")
                context.user_data['state'] = 'waiting_message_direct'
                return

            session_files = get_session_files()
            if not session_files:
                await update.message.reply_text("❌ Нет доступных сессий для получения списка контактов/чатов")
                return

            # Build targets per session so all loaded accounts are used
            targets_by_session = {}
            total_targets = 0
            for session_file in session_files:
                if spam_type == 'contacts':
                    contacts = await get_account_contacts(session_file)
                    ids = [c['id'] for c in contacts]
                else:
                    dialogs = await get_account_dialogs(session_file)
                    ids = [d['id'] for d in dialogs]
                if ids:
                    targets_by_session[session_file] = ids
                    total_targets += len(ids)

            if total_targets == 0:
                await update.message.reply_text(
                    "❌ Не найдено целей для рассылки у загруженных аккаунтов"
                )
                return

            # Keep flat list for compatibility with later UI/cost logic
            context.user_data['chats'] = [cid for ids in targets_by_session.values() for cid in ids]
            chats_count = total_targets
            context.user_data['targets_by_session'] = targets_by_session
        else:
            chats_count = len(context.user_data['chats'])

        if context.user_data['campaign_type'] == 'single_cycle':
            total_cost = chats_count * config.SINGLE_CYCLE_PRICE
            campaign_type_text = "Одна рассылка"
        else:
            cycles = context.user_data['cycles']
            total_cost = chats_count * cycles * config.MULTI_CYCLE_PRICE
            campaign_type_text = "Несколько рассылок"

        invoice = await create_invoice(total_cost, user)
        if not invoice:
            await update.callback_query.message.reply_text("❌ Ошибка при создании счета на оплату. Пожалуйста, попробуйте позже.")
            return

        context.user_data['invoice_id'] = invoice['invoice_id']
        context.user_data['total_cost'] = total_cost

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = [
            [InlineKeyboardButton("Запустить", callback_data=f'check_payment_{invoice["invoice_id"]}')],
            [InlineKeyboardButton("Назад", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        message_type_emoji = {
            'text': '📝',
            'photo': '🖼️',
            'video': '🎥',
            'document': '📄',
            'audio': '🎵',
            'voice': '🎤',
            'animation': '🎞️',
            'sticker': '🎭'
        }
        message_type = context.user_data.get('message_type', 'text')
        message_type_text_display = f"{message_type_emoji.get(message_type, '📝')} {message_type.capitalize()}"
        
        message_text = (
            f"📝 Рассылка:\n"
            f"• Тип: {campaign_type_text}\n"
            f"• Тип сообщения: {message_type_text_display}\n"
            f"• Чатов: {chats_count}\n"
            + (f"• Циклов: {cycles}\n" if context.user_data['campaign_type'] == 'multiple_cycles' else "")
            + (f"• Пауза между циклами: {context.user_data['cycle_interval']} мин\n" if context.user_data['campaign_type'] == 'multiple_cycles' else "")
            + f"\nНажми 'Запустить' чтобы начать"
        )

        if getattr(update, 'callback_query', None):
            await update.callback_query.message.reply_text(message_text, reply_markup=reply_markup)
        else:
            await update.message.reply_text(message_text, reply_markup=reply_markup)
    except Exception as e:
        error_message = "❌ Ошибка. Попробуй позже."
        if getattr(update, 'callback_query', None):
            await update.callback_query.message.reply_text(error_message)
        else:
            await update.message.reply_text(error_message)

async def handle_check_payment(update, context):
    query = update.callback_query
    invoice_id = query.data.replace('check_payment_', '')
    # Здесь можно добавить реальную проверку оплаты. Сейчас считаем, что оплата успешна.
    paid = await check_invoice(invoice_id)
    if not paid:
        await query.edit_message_text("❌ Оплата не подтверждена.")
        return

    user_id = query.from_user.id
    campaign_id = f"cmp_{int(time.time())}"

    campaign = {
        'id': campaign_id,
        'type': context.user_data.get('campaign_type', 'single_cycle'),
        'message': context.user_data.get('message', ''),
        'message_type': context.user_data.get('message_type', 'text'),
        'media_path': context.user_data.get('media_path', ''),
        'interval': context.user_data.get('interval', config.DEFAULT_INTERVAL),
        'chats': context.user_data.get('chats', []),
        'start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'end_time': '',
        'status': 'active',
        'user_id': user_id,
        'cycles': context.user_data.get('cycles', 0),
        'cycle_interval': context.user_data.get('cycle_interval', 0),
        'current_session': '',
        'targets_by_session': context.user_data.get('targets_by_session')
    }

    logger.info(f"Starting campaign {campaign_id} for user {user_id} | chats={len(campaign['chats'])} | type={campaign['type']}")
    save_campaign_to_db(campaign)

    if campaign['type'] == 'single_cycle':
        await query.edit_message_text("🚀 Запускаю рассылку...")
        await run_multithreaded_campaign(campaign, context.bot)
    else:
        cycles = campaign.get('cycles', 2)
        cycle_interval_min = campaign.get('cycle_interval', 1)
        await query.edit_message_text(f"🚀 Запускаю {cycles} циклов. Интервал: {cycle_interval_min} мин")
        for i in range(cycles):
            logger.info(f"Cycle {i+1}/{cycles} for campaign {campaign_id} starting")
            # Показываем финальный отчёт только на последнем цикле
            show_summary = (i == cycles - 1)
            await run_multithreaded_campaign(campaign, context.bot, show_summary=show_summary)
            if i < cycles - 1:
                # Сообщим о завершении текущего цикла (без кнопки)
                try:
                    await context.bot.send_message(
                        chat_id=campaign['user_id'],
                        text=f"Цикл {i+1} из {cycles} завершён. Ждём {cycle_interval_min} мин перед следующим."
                    )
                except Exception:
                    pass
                await asyncio.sleep(cycle_interval_min * 60)
                logger.info(f"Cycle {i+1} done, waiting {cycle_interval_min} min before next")

    # Очистим временные данные пользователя
    context.user_data.clear()