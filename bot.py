from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    ConversationHandler,
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
        'Hi! Send me your C code to compile (single-line or multi-line). I’ll run it interactively like a console.'
    )
    return CODE

async def handle_code(update: Update, context: CallbackContext) -> int:
    code = update.message.text
    context.user_data['code'] = code
    context.user_data['output'] = []
    context.user_data['inputs'] = []

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
        logger.info(f"Compilation stderr: {compile_result.stderr}")

        if compile_result.returncode == 0:
            logger.info("Compilation succeeded, starting interactive run")
            process = await asyncio.create_subprocess_exec("./temp", stdin=PIPE, stdout=PIPE, stderr=PIPE)
            context.user_data['process'] = process
            context.user_data['waiting_for_input'] = False
            asyncio.create_task(run_interactive(update, context))
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

async def run_interactive(update: Update, context: CallbackContext):
    process = context.user_data['process']
    output = context.user_data['output']

    while process.returncode is None:
        try:
            line = await process.stdout.readline()
            if line:
                line = line.decode().strip()
                logger.info(f"Program output: {line}")
                output.append(line)
                await update.message.reply_text(line)
                if line.endswith(": "):
                    context.user_data['waiting_for_input'] = True
                    return
            else:
                await process.wait()
                break
        except Exception as e:
            logger.error(f"Error reading output: {str(e)}")
            break

    await finish_program(update, context)

async def handle_input(update: Update, context: CallbackContext) -> int:
    if 'process' not in context.user_data or context.user_data['process'].returncode is not None:
        await update.message.reply_text("Program has finished or failed.")
        return ConversationHandler.END

    if not context.user_data.get('waiting_for_input', False):
        await update.message.reply_text("Not expecting input right now. Waiting for program output.")
        return RUNNING

    user_input = update.message.text
    context.user_data['inputs'].append(user_input)
    logger.info(f"Received input: {user_input}")

    process = context.user_data['process']
    try:
        process.stdin.write(f"{user_input}\n".encode())
        await process.stdin.drain()
        context.user_data['waiting_for_input'] = False
        asyncio.create_task(run_interactive(update, context))
        return RUNNING
    except Exception as e:
        logger.error(f"Error sending input: {str(e)}")
        await update.message.reply_text(f"Error processing input: {str(e)}")
        return ConversationHandler.END

async def finish_program(update: Update, context: CallbackContext):
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
        pdfkit.from_string(html_content,
