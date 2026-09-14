import os

OPENAI_API_KEY = "sk-78496c7a6eaa452b9edb123f2c4186fe"
OPENAI_BASE_URL = "https://api.deepseek.com"
MODEL_NAME = "deepseek-chat"

LLM_API_KEY = OPENAI_API_KEY
LLM_BASE_URL = OPENAI_BASE_URL
LLM_MODEL = MODEL_NAME

USER_DATA_DIR = os.path.abspath("./gs_user_data")
OUTPUT_FILE = "target_leads.xlsx"