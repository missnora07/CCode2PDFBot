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
CODE, RUNNING = range(2)

async def start(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text(
        'Hi! Send me your C code to compile (single-line or multi-line). '
        'I’ll run it like a console, prompting for input step-by-step.'
    )
    return CODE

async def handle_code(update: Update, context: CallbackContext) -> int:
    code = update.message.text
    context.user_data['code'] = code
    context.user_data['output'] = []
    context.user_data['inputs'] = []
    context.user_data['errors'] = []
    context.user_data['waiting_for_input'] = False
    
    try:
        logger.info("Raw received code:\n%s", code)
        
        formatted_code = code
        if '\n' not in code:
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
        
        if compile_result.returncode == 0:
            logger.info("Compilation succeeded, starting program execution")
            process = await asyncio.create_subprocess_exec(
                "stdbuf", "-o0", "./temp",
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE,
                env={"PYTHONUNBUFFERED": "1"}
            )
            
            context.user_data['process'] = process
            logger.info(f"Process started with PID: {process.pid}")
            asyncio.create_task(read_process_output(update, context))
            
            await update.message.reply_text(
                "Code compiled successfully! The program is now running. "
                "I’ll show prompts as they appear; send input when you see them. Type /cancel to stop."
            )
            return RUNNING
        else:
            error_msg = f"Compilation Error:\nSTDERR:\n{compile_result.stderr}"
            if compile_result.stdout:
                error_msg += f"\nSTDOUT:\n{compile_result.stdout}"
            logger.info("Compilation failed, sending error message")
            await update.message.reply_text(error_msg)
            return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error in handle_code: {str(e)}")
        await update.message.reply_text(f"An error occurred: {str(e)}")
        return ConversationHandler.END

async def read_process_output(update: Update, context: CallbackContext):
    process = context.user_data['process']
    output = context.user_data['output']
    errors = context.user_data['errors']
    
    logger.info("Starting to read process output")
    while process.returncode is None:
        try:
            stdout_line = await asyncio.wait_for(process.stdout.readline(), timeout=15.0)
            stdout_line = stdout_line.decode().rstrip()
            logger.info(f"Raw stdout: '{stdout_line}'")
            if stdout_line:
                output.append(stdout_line)
                await update.message.reply_text(stdout_line)
                if stdout_line.endswith(": ") or "enter" in stdout_line.lower():
                    context.user_data['waiting_for_input'] = True
                    logger.info("Detected input prompt, pausing for user input")
                    return
            
            stderr_line = await process.stderr.readline()
            stderr_line = stderr_line.decode().rstrip()
            if stderr_line:
                errors.append(stderr_line)
                await update.message.reply_text(f"Error: {stderr_line}")
        
        except asyncio.TimeoutError:
            logger.info("Timeout waiting for output, assuming input required")
            if process.returncode is None:
                context.user_data['waiting_for_input'] = True
                await update.message.reply_text("Program is waiting for input. Please provide it.")
                return
    
    # Capture any remaining output
    remaining_stdout = (await process.stdout.read()).decode().rstrip()
    remaining_stderr = (await process.stderr.read()).decode().rstrip()
    if remaining_stdout:
        output.append(remaining_stdout)
        await update.message.reply_text(remaining_stdout)
    if remaining_stderr:
        errors.append(remaining_stderr)
        await update.message.reply_text(f"Error: {remaining_stderr}")
    
    logger.info("Process completed, generating PDF")
    await generate_and_send_pdf(update, context)

async def handle_running(update: Update, context: CallbackContext) -> int:
    user_input = update.message.text
    process = context.user_data.get('process')
    
    if not process or process.returncode is not None:
        await update.message.reply_text("Program is not running anymore.")
        return ConversationHandler.END
    
    if not context.user_data.get('waiting_for_input', False):
        await update.message.reply_text("Program isn’t waiting for input right now. Please wait for a prompt.")
        return RUNNING
    
    try:
        process.stdin.write((user_input + "\n").encode())
        await process.stdin.drain()
        logger.info(f"Sent input to process: {user_input}")
        context.user_data['inputs'].append(user_input)
        context.user_data['waiting_for_input'] = False
        asyncio.create_task(read_process_output(update, context))
    except Exception as e:
        logger.error(f"Error sending input to process: {str(e)}")
        await update.message.reply_text(f"Failed to send input: {str(e)}")
    
    return RUNNING

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    try:
        code = context.user_data['code']
        output = context.user_data['output']
        inputs = context.user_data['inputs']
        errors = context.user_data['errors']
        
        # Merge output and inputs correctly
        full_output = []
        input_idx = 0
        for line in output:
            if (line.endswith(": ") or "enter" in line.lower()) and input_idx < len(inputs):
                full_output.append(f"{line}{inputs[input_idx]}")
                input_idx += 1
            else:
                full_output.append(line)
        
        full_output_str = "\n".join(full_output)
        errors_str = "\n".join(errors)
        
        logger.info(f"Preparing PDF - Code: {code}")
        logger.info(f"Preparing PDF - Full Output: {full_output_str}")
        logger.info(f"Preparing PDF - Errors: {errors_str}")
        
        html_content = f"""
        <html>
        <body>
            <h1>Source Code</h1>
            <pre><code>{code}</code></pre>
            <h1>Program Output</h1>
            <pre>{full_output_str if full_output_str else "No output captured"}</pre>
            <h1>Errors (if any)</h1>
            <pre>{errors_str if errors_str else "No errors"}</pre>
        </body>
        </html>
        """
        
        pdfkit.from_string(html_content, 'output.pdf')
        with open('output.pdf', 'rb') as pdf_file:
            await context.bot.send_document(chat_id=update.effective_chat.id, document=pdf_file)
        await update.message.reply_text("Program execution completed! Here's your PDF with the results.")
    
    except Exception as e:
        logger.error(f"Error in generate_and_send_pdf: {str(e)}")
        await update.message.reply_text(f"Failed to generate PDF: {str(e)}")
    
    finally:
        await cleanup(context)

async def cleanup(context: CallbackContext):
    process = context.user_data.get('process')
    if process and process.returncode is None:
        process.terminate()
        try:
            await process.wait()
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        logger.info("Process terminated during cleanup")
    
    for file in ["temp.c", "temp", "output.pdf"]:
        if os.path.exists(file):
            try:
                os.remove(file)
                logger.info(f"Cleaned up file: {file}")
            except OSError as e:
                logger.error(f"Failed to remove {file}: {str(e)}")
    
    context.user_data.clear()

async def cancel(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text("Operation cancelled.")
    await cleanup(context)
    return ConversationHandler.END

async def error_handler(update: Update, context: CallbackContext) -> None:
    logger.error("Exception occurred:", exc_info=context.error)
    try:
        await update.message.reply_text("An unexpected error occurred. Please try again later.")
    except Exception:
        pass
    await cleanup(context)

def main() -> None:
    application = Application.builder().token(TOKEN).build()
    
    loop = asyncio.get_event_loop()
    loop.run_until_complete(application.bot.set_webhook(url=None))
    logger.info("Webhook cleared to ensure clean polling start")
    
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code)],
            RUNNING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_running)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)
    
    application.run_polling()

if __name__ == '__main__':
    main()
