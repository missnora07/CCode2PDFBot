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

# Telegram Bot Token
TOKEN = os.getenv('TOKEN')

# States for ConversationHandler
CODE, INPUT = range(2)

def start(update: Update, context: CallbackContext) -> int:
    update.message.reply_text(
        'Hi! Send me your C code to compile. If your program needs input, I’ll ask for it after receiving the code.'
    )
    return CODE

def handle_code(update: Update, context: CallbackContext) -> int:
    code = update.message.text
    context.user_data['code'] = code
    
    # Write C code to a file
    with open("temp.c", "w") as file:
        file.write(code)
    
    try:
        # Compile C code
        compile_result = subprocess.run(["gcc", "temp.c", "-o", "temp"], capture_output=True, text=True)
        
        if compile_result.returncode == 0:
            update.message.reply_text("Code compiled successfully! Does your program need input? If yes, send it now. If no, type 'none'.")
            return INPUT
        else:
            update.message.reply_text(f"Compilation Error:\n{compile_result.stderr}")
            return ConversationHandler.END

    except Exception as e:
        update.message.reply_text(f"An error occurred: {str(e)}")
        return ConversationHandler.END

def handle_input(update: Update, context: CallbackContext) -> int:
    user_input = update.message.text
    code = context.user_data['code']
    
    try:
        if user_input.lower() == 'none':
            # Run without input
            run_result = subprocess.run(["./temp"], capture_output=True, text=True)
        else:
            # Run with input
            run_result = subprocess.run(["./temp"], input=user_input, capture_output=True, text=True)
        
        # Prepare HTML content with code, compilation output, and program output
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
            context.bot.send_document(chat_id=update.effective_chat.id, document=pdf_file)
        
        update.message.reply_text("Here’s your PDF with the code and output!")

    except Exception as e:
        update.message.reply_text(f"An error occurred during execution: {str(e)}")
    
    # Clean up
    for file in ["temp.c", "temp", "output.pdf"]:
        if os.path.exists(file):
            os.remove(file)
    
    return ConversationHandler.END

def cancel(update: Update, context: CallbackContext) -> int:
    update.message.reply_text("Operation cancelled.")
    # Clean up any leftover files
    for file in ["temp.c", "temp", "output.pdf"]:
        if os.path.exists(file):
            os.remove(file)
    return ConversationHandler.END

def main() -> None:
    """Start the bot."""
    # Use Application instead of Updater for v20+
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

    # Start polling
    application.run_polling()

if __name__ == '__main__':
    main()
