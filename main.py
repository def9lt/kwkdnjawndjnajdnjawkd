import os
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from config import BOT_TOKEN, logger
from utils.database import init_db
from handlers.start import start
from handlers.callback import button_handler
from handlers.message import handle_message

def main():
    if not os.path.exists('media'):
        os.makedirs('media')
    if not os.path.exists('sessions'):
        os.makedirs('sessions')
    
    init_db()
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

    logger.info("Bot started...")
    application.run_polling()

if __name__ == '__main__':
    main()