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
CODE, INPUT, RUNNING = range(3)

async def start(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text(
        'Hi! Send me your C code to compile (single-line or multi-line). '
        'If your program needs input during execution, I\'ll ask for it interactively.'
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
            
            # Start the process with pipes for interactive communication
            process = subprocess.Popen(
                ["./temp"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            
            context.user_data['process'] = process
            context.user_data['output'] = []
            context.user_data['errors'] = []
            
            # Start reading output in a non-blocking way
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
        # Read stdout line by line
        stdout_line = process.stdout.readline()
        if stdout_line:
            output.append(stdout_line)
            await update.message.reply_text(f"Program output:\n{stdout_line}")
        
        # Read stderr line by line
        stderr_line = process.stderr.readline()
        if stderr_line:
            errors.append(stderr_line)
            await update.message.reply_text(f"Program error:\n{stderr_line}")
        
        # Check if process has ended
        if process.poll() is not None:
            # Read any remaining output
            remaining_stdout, remaining_stderr = process.communicate()
            if remaining_stdout:
                output.append(remaining_stdout)
                await update.message.reply_text(f"Program output:\n{remaining_stdout}")
            if remaining_stderr:
                errors.append(remaining_stderr)
                await update.message.reply_text(f"Program error:\n{remaining_stderr}")
            
            # Generate and send PDF
            await generate_and_send_pdf(update, context)
            break
        
        await asyncio.sleep(0.1)  # Small delay to prevent busy waiting

async def handle_running(update: Update, context: CallbackContext) -> int:
    """Handle user input during program execution"""
    user_input = update.message.text
    process = context.user_data.get('process')
    
    if not process or process.poll() is not None:
        await update.message.reply_text("Program is not running anymore.")
        return ConversationHandler.END
    
    try:
        # Send input to the running process
        process.stdin.write(user_input + "\n")
        process.stdin.flush()
        logger.info(f"Sent input to process: {user_input}")
        
    except Exception as e:
        logger.error(f"Error sending input to process: {str(e)}")
        await update.message.reply_text(f"Failed to send input: {str(e)}")
    
    return RUNNING

async def generate_and_send_pdf(update: Update, context: CallbackContext):
    """Generate PDF with results and send to user"""
    try:
        code = context.user_data['code']
        output = "".join(context.user_data['output'])
        errors = "".join(context.user_data['errors'])
        
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
        cleanup(context)

def cleanup(context: CallbackContext):
    """Clean up resources"""
    process = context.user_data.get('process')
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
    
    for file in ["temp.c", "temp", "output.pdf"]:
        if os.path.exists(file):
            try:
                os.remove(file)
                logger.info(f"Cleaned up file: {file}")
            except OSError as e:
                logger.error(f"Failed to remove {file}: {str(e)}")

async def cancel(update: Update, context: CallbackContext) -> int:
    """Cancel the current operation"""
    await update.message.reply_text("Operation cancelled.")
    cleanup(context)
    return ConversationHandler.END

async def error_handler(update: Update, context: CallbackContext) -> None:
    """Handle errors"""
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
            RUNNING: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_running)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    application.add_handler(conv_handler)
    application.add_error_handler(error_handler)

    application.run_polling()

if __name__ == '__main__':
    main()
