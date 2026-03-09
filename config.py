import os
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

# Загрузка переменных окружения из .env или a.env
if os.path.exists('.env'):
	load_dotenv('.env')
elif os.path.exists('a.env'):
	load_dotenv('a.env')
else:
	load_dotenv()

# Токен бота
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()

# Параметры API для Telethon
API_ID = os.getenv('API_ID', '').strip()
API_HASH = os.getenv('API_HASH', '').strip()

# Константы рассылок
DEFAULT_INTERVAL = int(os.getenv('DEFAULT_INTERVAL', '3'))
MIN_CYCLES = int(os.getenv('MIN_CYCLES', '2'))
SINGLE_CYCLE_PRICE = float(os.getenv('SINGLE_CYCLE_PRICE', '0'))
MULTI_CYCLE_PRICE = float(os.getenv('MULTI_CYCLE_PRICE', '0'))

# Настройка логгера
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Формат логирования
log_format = logging.Formatter('[%(asctime)s] %(levelname)s %(name)s: %(message)s')

# Логи в файл с ротацией
file_handler = RotatingFileHandler('bot.log', maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
file_handler.setFormatter(log_format)
file_handler.setLevel(logging.INFO)

# Логи в консоль
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_format)
console_handler.setLevel(logging.INFO)

# Подключение хендлеров, избегая дублирования
if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
	logger.addHandler(file_handler)
if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler) for h in logger.handlers):
	logger.addHandler(console_handler)

# Валидация ключей
if not BOT_TOKEN:
	logger.error('BOT_TOKEN не задан. Укажите BOT_TOKEN в .env или a.env')
if not API_ID or not API_HASH:
	logger.warning('API_ID/API_HASH не заданы. Подключение Telethon может не работать.')
