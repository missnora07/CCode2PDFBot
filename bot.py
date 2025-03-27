def main() -> None:
    application = Application.builder().token(TOKEN).build()
    
    # Clear webhook synchronously before polling starts
    loop = asyncio.get_event_loop()
    loop.run_until_complete(application.bot.set_webhook(url=None))
    logger.info("Webhook cleared to ensure clean polling start")
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code)],
            RUNNING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)
    
    async def shutdown(application: Application):
        # Clean up any running processes
        for chat_id, context in application.user_data.items():
            if isinstance(context, dict) and 'process' in context and context['process'].returncode is None:
                try:
                    context['process'].terminate()
                    await context['process'].wait()
                    logger.info(f"Shutdown: Terminated process for chat {chat_id}")
                except Exception as e:
                    logger.error(f"Shutdown: Error terminating process for chat {chat_id}: {str(e)}")
        
        # Clean up files
        for file in ["temp.c", "temp", "output.pdf"]:
            if os.path.exists(file):
                try:
                    os.remove(file)
                    logger.info(f"Shutdown: Cleaned up file: {file}")
                except OSError as e:
                    logger.error(f"Shutdown: Failed to remove {file}: {str(e)}")
    
    application.add_handler(Application.shutdown(shutdown))
    
    # Run polling
    application.run_polling()

if __name__ == '__main__':
    main()
