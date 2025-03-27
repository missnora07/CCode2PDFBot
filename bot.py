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
import asyncio
from asyncio.subprocess import PIPE

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
CODE, RUNNING, INPUT = range(3)

async def start(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text(
        'Hi! Send me your C code to compile (single-line or multi-line). I’ll run it interactively like a console.'
    )
    return CODE

async def handle_code(update: Update, context: CallbackContext) -> int:
    code = update.message.text
    context.user_data['code'] = code
    context.user_data['output'] = []  # Store all output for PDF
    context.user_data['inputs'] = []  # Store user inputs
    
    try:
        logger.info("Raw received code:\n%s", code)
        
        # Normalize code: handle single-line or collapsed multi-line input
        formatted_code = code
        if '\n' not in code:  # Single-line input
            formatted_code = re.sub(r'(#include\s*<\w+\.h>)\s*', r'\1\n', code)
            formatted_code = re.sub(r'(int\s+main\(\)\s*\{)', r'\n\1', formatted_code)
            formatted_code = re.sub(r'(\{|\})', r'\1\n', formatted_code)
            formatted_code = re.sub(r'(;\s*)', r';\n', formatted_code)
            formatted_code = '\n'.join(line.strip() for line in formatted_code.splitlines() if line.strip())
        
        logger.info("Formatted code:\n%s", formatted_code)
        
        with open("temp.c", "w") as file:
            file.write(formatted_code)
        
        logger.info("Wrote code to temp.c")
        
        compile_result = subprocess.run(["gcc", "temp.c", "-o", "temp"], capture_output=True, text=True)
        
        logger.info(f"Compilation result - return code: {compile_result.returncode}")
        logger.info(f"Compilation stderr: {compile_result.stderr}")
        
        if compile_result.returncode == 0:
            logger.info("Compilation succeeded, starting interactive run")
            # Start the program interactively
            process = await asyncio.create_subprocess_exec("./temp", stdin=PIPE, stdout=PIPE, stderr=PIPE)
            context.user_data['process'] = process
            # Start reading output in the background
            asyncio.create_task(read_output(update, context))
            return RUNNING
        else:
            error_msg = f"Compilation Error:\nSTDERR:\n{compile_result.stderr}"
            if compile_result.stdout:
                error_msg += f"\nSTDOUT:\n{compile_result.stdout}"
            await update.message.reply_text(error_msg)
            return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error in handle_code: {str(e)}")
        await update.message.reply_text(f"An error occurred: {str(e)}")
        return ConversationHandler.END

async def read_output(update: Update, context: CallbackContext):
    """Read program output line-by-line and prompt for input."""
    process = context.user_data['process']
    output = context.user_data['output']
    
    while True:
        try:
            line = await process.stdout.readline()
            if not line:  # EOF
                break
            line = line.decode().strip()
            if line:
                logger.info(f"Program output: {line}")
                output.append(line)
                await update.message.reply_text(line)
                # Check if this is a prompt requiring input
                if line.endswith(": "):  # Heuristic for input prompts
                    context.user_data['waiting_for_input'] = True
                    return  # Wait for user input in INPUT state
        except Exception as e:
            logger.error(f"Error reading output: {str(e)}")
            break
    
    # Program finished
    await finish_program(update, context)

async def handle_input(update: Update, context: CallbackContext) -> int:
    if 'process' not in context.user_data or context.user_data['process'].returncode is not None:
        await update.message.reply_text("Program has finished or failed.")
        return ConversationHandler.END
    
    user_input = update.message.text
    context.user_data['inputs'].append(user_input)
    logger.info(f"Received input: {user_input}")
    
    process = context.user_data['process']
    try:
        # Send input to the program
        process.stdin.write(f"{user_input}\n".encode())
        await process.stdin.drain()
        context.user_data['waiting_for_input'] = False
        # Continue reading output
        asyncio.create_task(read_output(update, context))
        return RUNNING
    except Exception as e:
        logger.error(f"Error sending input: {str(e)}")
        await update.message.reply_text(f"Error processing input: {str(e)}")
        return ConversationHandler.END

async def finish_program(update: Update, context: CallbackContext):
    """Finish the program and generate PDF."""
    process = context.user_data['process']
    code = context.user_data['code']
    output = context.user_data['output']
    inputs = context.user_data['inputs']
    
    await process.wait()
    stderr = await process.stderr.read()
    stderr = stderr.decode().strip()
    
    logger.info(f"Program finished - return code: {process.returncode}")
    logger.info(f"Program stderr: {stderr}")
    
    if process.returncode != 0 and stderr:
        await update.message.reply_text(f"Runtime Error:\nSTDERR:\n{stderr}")
    else:
        # Combine output and inputs for PDF
        full_output = ""
        input_idx = 0
        for line in output:
            full_output += line + "\n"
            if line.endswith(": ") and input_idx < len(inputs):
                full_output += inputs[input_idx] + "\n"
                input_idx += 1
        
        html_content = f"""
        <html>
        <body>
            <h1>Source Code</h1>
            <pre><code>{code}</code></pre>
            <h1>Program Output</h1>
            <pre>{full_output}</pre>
            <h1>Errors (if any)</h1>
            <pre>{stderr}</pre>
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

    # Clean up
    for file in ["temp.c", "temp", "output.pdf"]:
        if os.path.exists(file):
            try:
                os.remove(file)
                logger.info(f"Cleaned up file: {file}")
            except OSError as e:
                logger.error(f"Failed to remove {file}: {str(e)}")
    
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: CallbackContext) -> int:
    if 'process' in context.user_data and context.user_data['process'].returncode is None:
        context.user_data['process'].terminate()
    await update.message.reply_text("Operation cancelled.")
    for file in ["temp.c", "temp", "output.pdf"]:
        if os.path.exists(file):
            try:
                os.remove(file)
            except OSError as e:
                logger.error(f"Failed to remove {file}: {str(e)}")
    context.user_data.clear()
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
            RUNNING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
            INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)

    application.run_polling()

if __name__ == '__main__':
    main()
