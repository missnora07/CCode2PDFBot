from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    ConversationHandler
)
import pdfkit
import subprocess
import os
import logging

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Telegram Bot Token
TOKEN = os.getenv('TOKEN')

# Debug: Print the token (remove this in production)
print(f"Loaded TOKEN: {TOKEN}")
if not TOKEN:
    raise ValueError("No TOKEN provided in environment variables!")

# States for ConversationHandler
CODE, INPUT = range(2)

async def start(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text(
        'Hi! Send me your C code to compile. If your program needs input, I’ll ask for it after receiving the code.'
    )
    return CODE

async def handle_code(update: Update, context: CallbackContext) -> int:
    code = update.message.text
    context.user_data['code'] = code
    
    try:
        # Write C code to a file
        with open("temp.c", "w") as file:
            file.write(code)
        
        # Compile C code
        compile_result = subprocess.run(["gcc", "temp.c", "-o", "temp"], capture_output=True, text=True)
        
        if compile_result.returncode == 0:
            await update.message.reply_text(
                "Code compiled successfully! Does your program need input? If yes, send it now. If no, type 'none'."
            )
            return INPUT
        else:
            await update.message.reply_text(f"Compilation Error:\n{compile_result.stderr}")
            return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error in handle_code: {str(e)}")
        await update.message.reply_text(f"An error occurred: {str(e)}")
        return ConversationHandler.END

async def handle_input(update: Update, context: CallbackContext) -> int:
    user_input = update.message.text
    code = context.user_data['code']
    
    try:
        if user_input.lower() == 'none':
            # Run without input
            run_result = subprocess.run(["./temp"], capture_output=True, text=True)
        else:
            # Run with input
            run_result = subprocess.run(["./temp"], input=user_input, capture_output=True, text=True)
        
        # Check for runtime errors
        if run_result.returncode != 0 and run_result.stderr:
            await update.message.reply_text(f"Runtime Error:\n{run_result.stderr}")
            return ConversationHandler.END
        
        # Prepare HTML content
        html_content = f"""
        <html>
        <body>
            <h1>Source Code</h1>
            <pre><code>{code}</code></pre>
            <h1>Compilation Output</h1>
            <pre>Success</pre>
            <h1>Program Output</h1>
            <pre>{run_result.stdout}</pre>
            <h1>Errors (if any)</h1>
            <pre>{run_result.stderr}</pre>
        </body>
        </html>
        """
        
        # Convert HTML to PDF
        pdfkit.from_string(html_content, 'output.pdf')
        
        # Send PDF
        with open('output.pdf', 'rb') as pdf_file:
            await context.bot.send_document(chat_id=update.effective_chat.id, document=pdf_file)
        
        await update.message.reply_text("Here’s your PDF with the code and output!")

    except subprocess.SubprocessError as e:
        logger.error(f"Subprocess error: {str(e)}")
        await update.message.reply_text(f"Execution failed: {str(e)}")
    except Exception as e:
        logger.error(f"Error in handle_input: {str(e)}")
        await update.message.reply_text(f"An error occurred during execution: {str(e)}")
    
    finally:
        # Clean up
        for file in ["temp.c", "temp", "output.pdf"]:
            if os.path.exists(file):
                try:
                    os.remove(file)
                except OSError as e:
                    logger.error(f"Failed to remove {file}: {str(e)}")
    
    return ConversationHandler.END

async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("Operation cancelled.")
    # Clean up any leftover files
    for file in ["temp.c", "temp", "output.pdf"]:
        if os.path.exists(file):
            try:
                os.remove(file)
            except OSError as e:
                logger.error(f"Failed to remove {file}: {str(e)}")
    return ConversationHandler.END

async def error_handler(update: Update, context: CallbackContext) -> None:
    """Log unhandled errors and notify the user."""
    logger.error("Exception occurred:", exc_info=context.error)
    try:
        await update.message.reply_text("An unexpected error occurred. Please try again later.")
    except Exception:
        pass  # If we can't reply, just log it

def main() -> None:
    """Start the bot."""
    application = Application.builder().token(TOKEN).build()
    
    # Set up the ConversationHandler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code)],
            INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)

    # Start polling
    application.run_polling()

if __name__ == '__main__':
    main()
