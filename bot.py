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
        'If your program needs input during execution, I’ll ask for it interactively.'
    )
    return CODE

async def handle_code(update: Update, context: CallbackContext) -> int:
    code = update.message.text
    context.user_data['code'] = code
    context.user_data['output'] = []
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
        
        logger.info(f"Compilation result - return code: {compile_result.returncode}")
        logger.info(f"Compilation stdout: {compile_result.stdout}")
        logger.info(f"Compilation stderr: {compile_result.stderr}")
        
        if compile_result.returncode == 0:
            logger.info("Compilation succeeded, starting program execution")
            process = await asyncio.create_subprocess_exec(
                "./temp",
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE
            )
            
            context.user_data['process'] = process
            asyncio.create_task(read_process_output(update, context))
            
            await update.message.reply_text(
                "Code compiled successfully! The program is now running. "
                "Send input when prompted by the program. Type /cancel to stop."
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
    
    while process.returncode is None:
        try:
            stdout_task = asyncio.create_task(process.stdout.readline())
            stderr_task = asyncio.create_task(process.stderr.readline())
            done, pending = await asyncio.wait(
                [stdout_task, stderr_task],
                timeout=5.0,
                return_when=asyncio.FIRST_COMPLETED
            )
            
            if stdout_task in done:
                stdout_line = (await stdout_task).decode().strip()
                if stdout_line:
                    output.append(stdout_line)
                    await update.message.reply_text(stdout_line)
                    if stdout_line.endswith(": "):  # Detect input prompt
                        context.user_data['waiting_for_input'] = True
                        for task in pending:
                            task.cancel()
                        return  # Wait for user input
            
            if stderr_task in done:
                stderr_line = (await stderr_task).decode().strip()
                if stderr_line:
                    errors.append(stderr_line)
                    await update.message.reply_text(f"Error: {stderr_line}")
            
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            
            if not done and not context.user_data['waiting_for_input']:
                logger.warning("No output within 5 seconds, checking process status")
                await process.wait()  # Force check if process has ended silently
                if process.returncode is not None:
                    break
        
        except Exception as e:
            logger.error(f"Error in read_process_output: {str(e)}")
            await update.message.reply_text(f"Execution error: {str(e)}")
            break
    
    # Process has ended, read remaining output and finalize
    remaining_stdout = (await process.stdout.read()).decode().strip()
    remaining_stderr = (await process.stderr.read()).decode().strip()
    if remaining_stdout:
        output.append(remaining_stdout)
        await update.message.reply_text(remaining_stdout)
    if remaining_stderr:
        errors.append(remaining_stderr)
        await update.message.reply_text(f"Error: {remaining_stderr}")
    
    await generate_and_send_pdf(update, context)

async def handle_running(update: Update, context: CallbackContext) -> int:
    user_input = update.message.text
    process = context.user_data.get('process')
    
    if not process or process.returncode is not None:
        await update.message.reply_text("Program is not running anymore.")
        return ConversationHandler.END
    
    if not context.user_data.get('waiting_for_input', False):
        await update.message.reply_text("Program isn’t waiting for input right now.")
        return RUNNING
    
    try:
        process.stdin.write((user_input + "\n").encode())
        await process.stdin.drain()
        logger.info(f"Sent input to process: {user_input}")
        context.user_data['waiting_for_input'] = False
        asyncio.create_task(read_process_output(update, context))  # Resume reading
    except Exception as e:
        logger.error(f"Error sending input to process: {str(e)}")
        await update.message.reply_text(f"Failed to send input: {str(e)}")
    
    return RUNNING

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    try:
        code = context.user_data['code']
        output = "\n".join(context.user_data['output'])
        errors = "\n".join(context.user_data['errors'])
        
        html_content = f"""
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
        
        logger.info("Generating PDF...")
        pdfkit.from_string(html_content, 'output.pdf')
        logger.info("PDF generated successfully")
        
        if not os.path.exists('output.pdf'):
            raise FileNotFoundError("PDF file was not created")
        
        with open('output.pdf', 'rb') as pdf_file:
            logger.info("Sending PDF to user...")
            await context.bot.send_document(chat_id=update.effective_chat.id, document=pdf_file)
            logger.info("PDF sent successfully")
        
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
