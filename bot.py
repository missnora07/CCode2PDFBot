from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    ConversationHandler
)
import subprocess
import os
import logging
import re
import asyncio
from asyncio.subprocess import PIPE

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv('TOKEN')
if not TOKEN:
    raise ValueError("No TOKEN provided in environment variables!")

CODE, RUNNING = range(2)

async def start(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text(
        'Hi! Send me your C code to compile. I’ll run it like a console, prompting for input step-by-step.'
    )
    return CODE

async def handle_code(update: Update, context: CallbackContext) -> int:
    code = update.message.text
    context.user_data.clear()
    context.user_data.update({'code': code, 'output': [], 'inputs': [], 'errors': [], 'waiting_for_input': False})

    try:
        with open("temp.c", "w") as file:
            file.write(code)
        
        compile_result = subprocess.run(["gcc", "temp.c", "-o", "temp"], capture_output=True, text=True)

        if compile_result.returncode != 0:
            await update.message.reply_text(f"Compilation Error:\n{compile_result.stderr}")
            return ConversationHandler.END
        
        process = await asyncio.create_subprocess_exec("./temp", stdin=PIPE, stdout=PIPE, stderr=PIPE)
        context.user_data['process'] = process
        asyncio.create_task(read_process_output(update, context))
        
        await update.message.reply_text("Code compiled successfully! The program is running. Type /cancel to stop.")
        return RUNNING
    
    except Exception as e:
        await update.message.reply_text(f"An error occurred: {str(e)}")
        return ConversationHandler.END

async def read_process_output(update: Update, context: CallbackContext):
    process = context.user_data['process']
    while process.returncode is None:
        stdout_line = (await process.stdout.readline()).decode().strip()
        stderr_line = (await process.stderr.readline()).decode().strip()
        
        if stdout_line:
            await update.message.reply_text(stdout_line)
            context.user_data['output'].append(stdout_line)
            if stdout_line.endswith(":") or "enter" in stdout_line.lower():
                context.user_data['waiting_for_input'] = True
                return
        
        if stderr_line:
            await update.message.reply_text(f"Error: {stderr_line}")
            context.user_data['errors'].append(stderr_line)

    await generate_and_send_pdf(update, context)

async def handle_running(update: Update, context: CallbackContext) -> int:
    user_input = update.message.text
    process = context.user_data.get('process')
    
    if not process or process.returncode is not None:
        await update.message.reply_text("Program is not running anymore.")
        return ConversationHandler.END
    
    if not context.user_data.get('waiting_for_input', False):
        await update.message.reply_text("Program isn't waiting for input right now. Please wait for a prompt.")
        return RUNNING
    
    process.stdin.write((user_input + "\n").encode())
    await process.stdin.drain()
    context.user_data['inputs'].append(user_input)
    context.user_data['waiting_for_input'] = False
    
    asyncio.create_task(read_process_output(update, context))
    return RUNNING

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    code = context.user_data['code']
    output = "\n".join(context.user_data['output'])
    errors = "\n".join(context.user_data['errors'])
    
    pdf_content = f"""
    <html>
    <body>
        <h1>Source Code</h1>
        <pre><code>{code}</code></pre>
        <h1>Program Output</h1>
        <pre>{output}</pre>
        <h1>Errors (if any)</h1>
        <pre>{errors}</pre>
    </body>
    </html>
    """
    
    with open("output.pdf", "wb") as f:
        f.write(pdf_content.encode())
    
    with open('output.pdf', 'rb') as pdf_file:
        await context.bot.send_document(chat_id=update.effective_chat.id, document=pdf_file)
    
    await update.message.reply_text("Execution completed! Here's your PDF with results.")
    await cleanup(context)

async def cleanup(context: CallbackContext):
    process = context.user_data.get('process')
    if process and process.returncode is None:
        process.terminate()
    for file in ["temp.c", "temp", "output.pdf"]:
        if os.path.exists(file):
            os.remove(file)
    context.user_data.clear()

async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("Operation cancelled.")
    await cleanup(context)
    return ConversationHandler.END

def main() -> None:
    application = Application.builder().token(TOKEN).build()
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code)],
            RUNNING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_running)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    application.add_handler(conv_handler)
    application.run_polling()

if __name__ == '__main__':
    main()
