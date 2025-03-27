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
CODE, RUNNING = range(2)  # Removed unused INPUT state

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
        logger.info(f"Compilation stdout: {compile_result.stdout}")
        logger.info(f"Compilation stderr: {compile_result.stderr}")
        
        if compile_result.returncode == 0:
            logger.info("Compilation succeeded, starting program execution")
            
            # Start the process with asyncio pipes for interactive communication
            process = await asyncio.create_subprocess_exec(
                "./temp",
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE
            )
            
            context.user_data['process'] = process
            
            # Start reading output in the background
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
    """Continuously read output from the process and send to user"""
    process = context.user_data['process']
    output = context.user_data['output']
    errors = context.user_data['errors']
    
    while True:
        try:
            # Read stdout and stderr concurrently with a timeout
            stdout_task = asyncio.create_task(process.stdout.readline())
            stderr_task = asyncio.create_task(process.stderr.readline())
            done, pending = await asyncio.wait(
                [stdout_task, stderr_task],
                timeout=5.0,
                return_when=asyncio.FIRST_COMPLETED
            )
            
            # Handle stdout
            if stdout_task in done:
                stdout_line = (await stdout_task).decode().strip()
                if stdout_line:
                    output.append(stdout_line)
                    await update.message.reply_text(stdout_line)
            
            # Handle stderr
            if stderr_task in done:
                stderr_line = (await stderr_task).decode().strip()
                if stderr_line:
                    errors.append(stderr_line)
                    await update.message.reply_text(f"Error: {stderr_line}")
            
            # Cancel pending tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            
            # Check if process has ended
            if process.returncode is not None:
                # Read any remaining output
                remaining_stdout = (await process.stdout.read()).decode().strip()
                remaining_stderr = (await process.stderr.read()).decode().strip()
                if remaining_stdout:
                    output.append(remaining_stdout)
                    await update.message.reply_text(remaining_stdout)
                if remaining_stderr:
                    errors.append(remaining_stderr)
                    await update.message.reply_text(f"Error: {remaining_stderr}")
                
                # Generate and send PDF
                await generate_and_send_pdf(update, context)
                break
            
            # If no output within timeout, check process status
            if not done:
                logger.warning("No output within 5 seconds, checking process status")
                if process.returncode is not None:
                    await generate_and_send_pdf(update, context)
                    break
        
        except Exception as e:
            logger.error(f"Error in read_process_output: {str(e)}")
            await update.message.reply_text(f"Execution error: {str(e)}")
            break

async def handle_running(update: Update, context: CallbackContext) -> int:
    """Handle user input during program execution"""
    user_input = update.message.text
    process = context.user_data.get('process')
    
    if not process or process.returncode is not None:
        await update.message.reply_text("Program is not running anymore.")
        return ConversationHandler.END
    
    try:
        # Send input to the running process
        process.stdin.write((user_input + "\n").encode())
        await process.stdin.drain()
        logger.info(f"Sent input to process: {user_input}")
        
    except Exception as e:
        logger.error(f"Error sending input to process: {str(e)}")
        await update.message.reply_text(f"Failed to send input: {str(e)}")
    
    return RUNNING

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    """Generate PDF with results and send to user"""
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
    """Clean up resources asynchronously"""
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
    """Cancel the current operation"""
    await update.message.reply_text("Operation cancelled.")
    await cleanup(context)
    return ConversationHandler.END

async def error_handler(update: Update, context: CallbackContext) -> None:
    """Handle errors"""
    logger.error("Exception occurred:", exc_info=context.error)
    try:
        await update.message.reply_text("An unexpected error occurred. Please try again later.")
    except Exception:
        pass
    await cleanup(context)

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
            RUNNING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_running)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)
    
    # Run polling synchronously, letting it manage the event loop
    application.run_polling()

if __name__ == '__main__':
    main()
