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
import re

# Set up logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Telegram Bot Token
TOKEN = os.getenv('TOKEN')

print(f"Loaded TOKEN: {TOKEN}")
if not TOKEN:
    raise ValueError("No TOKEN provided in environment variables!")

# States for ConversationHandler
CODE, INPUT = range(2)

async def start(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text(
        'Hi! Send me your C code to compile (single-line or multi-line). If your program needs input, I’ll ask for it.'
    )
    return CODE

async def handle_code(update: Update, context: CallbackContext) -> int:
    code = update.message.text
    context.user_data['code'] = code
    
    try:
        logger.info("Raw received code:\n%s", code)
        
        # Normalize code: handle single-line or collapsed multi-line input
        formatted_code = code
        if '\n' not in code:  # Single-line input
            # Split #include directives and major C syntax elements
            formatted_code = re.sub(r'(#include\s*<\w+\.h>)', r'\1\n', code)  # #include on its own line
            formatted_code = re.sub(r'(\bint\s+main\(\)\s*\{)', r'\n\1', formatted_code)  # main() on new line
            formatted_code = re.sub(r'(\{|\})', r'\1\n', formatted_code)  # Braces on new lines
            formatted_code = re.sub(r'(;\s*)', r';\n', formatted_code)  # Semicolons followed by newlines
            # Clean up extra newlines and whitespace
            formatted_code = '\n'.join(line.strip() for line in formatted_code.splitlines() if line.strip())
        
        logger.info("Formatted code:\n%s", formatted_code)
        
        # Write the code to temp.c
        with open("temp.c", "w") as file:
            file.write(formatted_code)
        
        logger.info("Wrote code to temp.c")
        
        # Compile C code
        compile_result = subprocess.run(["gcc", "temp.c", "-o", "temp"], capture_output=True, text=True)
        
        logger.info(f"Compilation result - return code: {compile_result.returncode}")
        logger.info(f"Compilation stdout: {compile_result.stdout}")
        logger.info(f"Compilation stderr: {compile_result.stderr}")
        
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
        logger.info(f"Received input: {user_input}")
        
        if user_input.lower() == 'none':
            run_result = subprocess.run(["./temp"], capture_output=True, text=True)
        else:
            run_result = subprocess.run(["./temp"], input=user_input, capture_output=True, text=True)
        
        logger.info(f"Program stdout: {run_result.stdout}")
        logger.info(f"Program stderr: {run_result.stderr}")
        logger.info(f"Program return code: {run_result.returncode}")
        
        if run_result.returncode != 0 and run_result.stderr:
            await update.message.reply_text(f"Runtime Error:\n{run_result.stderr}")
            return ConversationHandler.END
        
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
        
        logger.info("Generating PDF...")
        pdfkit.from_string(html_content, 'output.pdf')
        logger.info("PDF generated successfully")
        
        if not os.path.exists('output.pdf'):
            raise FileNotFoundError("PDF file was not created")
        
        with open('output.pdf', 'rb') as pdf_file:
            logger.info("Sending PDF to user...")
            await context.bot.send_document(chat_id=update.effective_chat.id, document=pdf_file)
            logger.info("PDF sent successfully")
        
        await update.message.reply_text("Here’s your PDF with the code and output!")

    except subprocess.SubprocessError as e:
        logger.error(f"Subprocess error: {str(e)}")
        await update.message.reply_text(f"Execution failed: {str(e)}")
    except Exception as e:
        logger.error(f"Error in handle_input: {str(e)}")
        await update.message.reply_text(f"An error occurred during execution: {str(e)}")
    
    finally:
        for file in ["temp.c", "temp", "output.pdf"]:
            if os.path.exists(file):
                try:
                    os.remove(file)
                    logger.info(f"Cleaned up file: {file}")
                except OSError as e:
                    logger.error(f"Failed to remove {file}: {str(e)}")
    
    return ConversationHandler.END

async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("Operation cancelled.")
    for file in ["temp.c", "temp", "output.pdf"]:
        if os.path.exists(file):
            try:
                os.remove(file)
            except OSError as e:
                logger.error(f"Failed to remove {file}: {str(e)}")
    return ConversationHandler.END

async def error_handler(update: Update, context: CallbackContext) -> None:
    logger.error("Exception occurred:", exc_info=context.error)
    try:
        await update.message.reply_text("An unexpected error occurred. Please try again later.")
    except Exception:
        pass

def main() -> None:
    application = Application.builder().token(TOKEN).build()
    
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

    application.run_polling()

if __name__ == '__main__':
    main()
