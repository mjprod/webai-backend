from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from llama_cpp import Llama
import os
import random
from databases import Database
import sqlalchemy
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func
from .data.agents import AGENTS
import re
import openai
from dotenv import load_dotenv
import os

load_dotenv()

# --- CONFIG ---

app = FastAPI(title="WebAI Backend with Knowledge & History (SQLite) 🚀", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "Phi-3-mini-4k-instruct-q4.gguf")


llm = Llama(model_path=MODEL_PATH, n_ctx=4096, n_gpu_layers=-1)
DATABASE_URL = "sqlite:///./chat_history.db"
database = Database(DATABASE_URL)
metadata = sqlalchemy.MetaData()

# llm_validator = Llama(model_path=MODEL_VALIDATOR_PATH, n_ctx=4096, n_gpu_layers=-1)
openai_api_key = os.getenv("OPENAI_API_KEY")

openai.api_key = openai_api_key

# --- DB TABLES ---

chats = sqlalchemy.Table(
    "chats",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("conversation_id", sqlalchemy.String, nullable=False, index=True),
    sqlalchemy.Column("role", sqlalchemy.String),
    sqlalchemy.Column("message", sqlalchemy.String),
    sqlalchemy.Column("agent_id", sqlalchemy.String, nullable=True),
    sqlalchemy.Column("timestamp", sqlalchemy.DateTime, nullable=False, server_default=func.now()),
)

knowledge = sqlalchemy.Table(
    "knowledge",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("conversation_id", sqlalchemy.String, nullable=False, index=True),
    sqlalchemy.Column("key", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("value", sqlalchemy.String, nullable=False),
    sqlalchemy.Column("timestamp", sqlalchemy.DateTime, nullable=False, server_default=func.now()),
)

engine = sqlalchemy.create_engine(DATABASE_URL)
metadata.create_all(engine)

# --- APP EVENTS ---

name_retry_phrases = [
    "I didn't catch your name clearly. Could you please tell me your name again?",
    "Sorry, I missed your name. Can you type it again for me?",
    "Oops, I didn't get your name. Could you repeat your name for me?",
    "Would you mind telling me your name one more time?",
    "Could you please clarify your name for me?",
    "I'm having trouble hearing your name. Could you share it again?",
    "Sorry, I couldn't understand your name. Can you write it again?",
    "Just to confirm, what's your name?",
    "Apologies, I didn't catch your name. Can you let me know your name again?",
]

# --- REQUEST MODEL ---
class Prompt(BaseModel):
    user_input: str = Field(..., description="The user's message", example="Hello!")
    conversation_id: str = Field(..., min_length=1, description="Conversation ID", example="001")
    agent_id: str = Field("max", description="Agent identifier", example="max")

# --- APP EVENTS ---
@app.on_event("startup")
async def startup():
    await database.connect()

@app.on_event("shutdown")
async def shutdown():
    await database.disconnect()

@app.get("/")
async def health_check():
    return {"status": "✅ WebAI Backend running with SQLite history and knowledge storage!"}


# --- HELPERS ---d
def format_history(rows, last_n=5):
    rows = sorted(rows, key=lambda x: x["timestamp"])
    history = [
        f"{'User' if row['role'].startswith('user') else 'Agent'}: {row['message']}"
        for row in rows[-last_n:]
    ]
    return "\n".join(history)

async def save_knowledge(conversation_id: str, key: str, value: str):
    await database.execute(knowledge.insert().values(
        conversation_id=conversation_id,
        key=key,
        value=value
    ))

async def get_knowledge(conversation_id: str):
    query = knowledge.select().where(knowledge.c.conversation_id == conversation_id)
    rows = await database.fetch_all(query)
    return {row["key"]: row["value"] for row in rows}


async def extract_name_with_validator(user_input: str, entity_type: str = "person's name"):
    prompt = (
        f"Extract ONLY the {entity_type} from this message. "
        f"If there is no valid {entity_type}, just return an empty string.\n"
        f"Message: {user_input}\n"
        f"{entity_type.capitalize()}:"
    )

    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "You extract structured data from user messages. Respond with only the data, no explanation. if don't find the data, return None "},
            {"role": "user", "content": prompt},
        ],
        max_tokens=8,
        temperature=0,
        stop=["\n"]
    )
    answer = response["choices"][0]["message"]["content"].strip()
    print(f"[OPENAI VALIDATOR] Extraction for '{user_input}': '{answer}'")

    if not answer or "none" in answer.lower():
        return ""
    return answer.strip()

# --- CORE LOGIC ---

async def generate_response(prompt: Prompt, user_role: str, agent_role: str, agent_name: str, agent_definition: str):
    if not prompt.conversation_id:
        raise HTTPException(status_code=400, detail="conversation_id is required")

    rows = await database.fetch_all(chats.select().where(chats.c.conversation_id == prompt.conversation_id))
    knowledge_data = await get_knowledge(prompt.conversation_id)

    if prompt.user_input.strip():
        await database.execute(chats.insert().values(
            conversation_id=prompt.conversation_id,
            role=user_role,
            message=prompt.user_input,
            agent_id=prompt.agent_id
        ))

    # --- Atualização NATURAL de nome/empresa (antes de tudo) ---
    def extract_name_update(message):
        m = re.search(r"(my name is|call me|change my name to)\s+([A-Za-zÁÉÍÓÚÃÕÇ][a-záéíóúãõç ]+)", message, re.IGNORECASE)
        if m:
            return m.group(2).strip()
        return None

    def extract_company_update(message):
        m = re.search(r"(my company is|company is|company now is)\s+([A-Za-z0-9ÁÉÍÓÚÃÕÇáéíóúãõç &.\\-]+)", message, re.IGNORECASE)
        if m:
            return m.group(2).strip()
        return None


    name_update = extract_name_update(prompt.user_input)

    if "name" not in knowledge_data and len(prompt.user_input.strip()) > 0 and rows:
        # Tenta extrair o nome (se veio num comando OU frase)
        name_extracted = name_update or await extract_name_with_validator(prompt.user_input, "person's name or nickname")
        if name_extracted:
            await save_knowledge(prompt.conversation_id, "name", name_extracted)

            company_question_phrases = [
                f"Nice to meet you, {name_extracted}! What's your company's name?",
                f"Great to meet you, {name_extracted}. May I know your company name?",
                f"Thanks, {name_extracted}! Could you tell me which company you're with?",
                f"Pleasure meeting you, {name_extracted}. What's the name of your company?",
                f"Awesome, {name_extracted}! Which company do you represent?",
                f"Hi {name_extracted}, and what's your company called?",
                f"Thank you, {name_extracted}. Could you please share your company's name?",
                f"Glad to connect, {name_extracted}! What's your company?",
            ]
            company_question = random.choice(company_question_phrases)

            await database.execute(chats.insert().values(
                conversation_id=prompt.conversation_id,
                role=agent_role,
                message=company_question,
                agent_id=prompt.agent_id
            ))
            return {
                "response": company_question,
                "history": [{
                    "id": row["id"],
                    "role": row["role"],
                    "message": row["message"],
                    "agent_id": row["agent_id"],
                    "timestamp": row["timestamp"].isoformat()
                } for row in rows],
                "knowledge": await get_knowledge(prompt.conversation_id)
            }
        else:
            retry_message = random.choice(name_retry_phrases)
            await database.execute(chats.insert().values(
                conversation_id=prompt.conversation_id,
                role=agent_role,
                message=retry_message,
                agent_id=prompt.agent_id
            ))
            return {
                "response": retry_message,
                "history": [{
                    "id": row["id"],
                    "role": row["role"],
                    "message": row["message"],
                    "agent_id": row["agent_id"],
                    "timestamp": row["timestamp"].isoformat()
                } for row in rows],
                "knowledge": knowledge_data
            }
    company_update = extract_company_update(prompt.user_input)

    if company_update and knowledge_data.get("company") and len(prompt.user_input.strip()) > 0 and rows:
        await save_knowledge(prompt.conversation_id, "company", company_update)
        confirmation = f"Company updated to '{company_update}'! How can I help you today?"
        await database.execute(chats.insert().values(
            conversation_id=prompt.conversation_id,
            role=agent_role,
            message=confirmation,
            agent_id=prompt.agent_id
        ))
        return {"response": confirmation, "history": [{
                    "id": row["id"],
                    "role": row["role"],
                    "message": row["message"],
                    "agent_id": row["agent_id"],
                    "timestamp": row["timestamp"].isoformat()
                } for row in rows], "knowledge": await get_knowledge(prompt.conversation_id)}

    # --- Check if the agent is already in conversation ---
    if rows:
        last_agent_msg = next(
            (row for row in reversed(rows) if row["role"].startswith("agent")), None
        )

        # 1. Só tem nome, falta empresa
        if (
            last_agent_msg
            and last_agent_msg["agent_id"] != prompt.agent_id
            and knowledge_data.get("name")
            and not knowledge_data.get("company")
        ):
            last_agent_name = AGENTS.get(last_agent_msg["agent_id"], {}).get("name", "our team")
            ask_company = (
                f"Hi {knowledge_data['name']}, now you are talking to {agent_name}. "
                f"I'll continue helping you from where {last_agent_name} left off. What's your company's name?"
            )
            await database.execute(chats.insert().values(
                conversation_id=prompt.conversation_id,
                role=agent_role,
                message=ask_company,
                agent_id=prompt.agent_id
            ))
            return {
                "response": ask_company,
                "history": [{
                    "id": row["id"],
                    "role": row["role"],
                    "message": row["message"],
                    "agent_id": row["agent_id"],
                    "timestamp": row["timestamp"].isoformat()
                } for row in rows],
                "knowledge": knowledge_data
            }

        # 2. Já tem nome e empresa
        elif (
            last_agent_msg
            and last_agent_msg["agent_id"] != prompt.agent_id
            and knowledge_data.get("name")
            and knowledge_data.get("company")
        ):
            last_agent_name = AGENTS.get(last_agent_msg["agent_id"], {}).get("name", "our team")
            continuity_message = (
                f"Hi {knowledge_data['name']}, now you are talking to {agent_name}. "
                f"I'll continue helping you from where {last_agent_name} left off."
            )

            history_text = format_history(rows)
            last_user_msg = next(
                (row for row in reversed(rows) if row["role"].startswith("user")), None
            )
            
            # Instrução extra para não se reapresentar
            instruction = (
                f"{agent_definition}\n"
                "IMPORTANT: The user already knows your name and role. "
                "Do NOT introduce yourself again or greet. Just continue the conversation based on the previous context."
            )

            if last_user_msg:
                full_prompt = (
                    f"{instruction}\nContext:\n{history_text}\nUser: {last_user_msg['message']}\nAgent:"
                )
            else:
                full_prompt = f"{instruction}\nContext:\n{history_text}\nAgent:"

            ai_response = llm(full_prompt, max_tokens=650, temperature=0.7, stop=["User:", "\n"])
            response_text = ai_response['choices'][0]['text'].strip()

            # Se vier saudação, remove a primeira linha
            for prefix in ("hi", "hello", "my name is", "i am"):
                if response_text.lower().startswith(prefix):
                    response_text = '\n'.join(response_text.split('\n')[1:]).strip()
                    break

            combined_message = f"{continuity_message} {response_text}"

            await database.execute(chats.insert().values(
                conversation_id=prompt.conversation_id,
                role=agent_role,
                message=combined_message,
                agent_id=prompt.agent_id
            ))
            return {
                "response": combined_message,
                "history": [{
                    "id": row["id"],
                    "role": row["role"],
                    "message": row["message"],
                    "agent_id": row["agent_id"],
                    "timestamp": row["timestamp"].isoformat()
                } for row in rows],
                "knowledge": knowledge_data
            }

    # Start conversation
    if not rows:
        initial_phrases = [
            f"Hey, I am {agent_name} from M and J Intelligence, how are you? Who am I speaking with?",
            f"Hello! Thanks for picking up, I'm {agent_name} from M and J Intelligence. Who’s on the line today?",
            f"Hi there!, I'm {agent_name} from M and J Intelligence. Anyway What's your name?",
            f"Hey, I'm {agent_name} from M and J Intelligence, great to hear from you! What should I call you?",
            f"Hi, I'm {agent_name} from M and J Intelligence, hope you're having a fantastic day! What's your name?",
            f"Hello, I'm {agent_name} from M and J Intelligence, it's a pleasure to connect! Who do I have the pleasure of speaking with?",
            f"Hey there, I'm {agent_name} from M and J Intelligence, how's everything going? Who am I chatting with today?",
        ]
        initial_message = random.choice(initial_phrases)
        await database.execute(chats.insert().values(
            conversation_id=prompt.conversation_id,
            role=agent_role,
            message=initial_message,
            agent_id=prompt.agent_id
        ))
        return {"response": initial_message, "history": [{
                "id": row["id"],
                "role": row["role"],
                "message": row["message"],
                "agent_id": row["agent_id"],
                "timestamp": row["timestamp"].isoformat()
            } for row in rows], "knowledge": knowledge_data}

    # Ask for Name (onboarding)
    if "name" not in knowledge_data:
        name_extracted = await extract_name_with_validator(prompt.user_input, "person's name or nickname")
        if name_extracted:
            await save_knowledge(prompt.conversation_id, "name", name_extracted)
            company_question_phrases = [
                f"Nice to meet you, {name_extracted}! What's your company's name?",
                f"Great to meet you, {name_extracted}. Which company are you with?",
                f"Thanks, {name_extracted}! Could you tell me which company you're representing?",
                f"Pleasure meeting you, {name_extracted}. What's the name of your company?",
                f"Awesome, {name_extracted}! Which company do you represent?",
                f"Hi {name_extracted}, and what's your company called?",
                f"Thank you, {name_extracted}. Could you please share your company's name?",
                f"Glad to connect, {name_extracted}! What's your company?",
                f"{name_extracted}, may I have your company name?",
            ]
            company_question = random.choice(company_question_phrases)

            await database.execute(chats.insert().values(
                conversation_id=prompt.conversation_id,
                role=agent_role,
                message=company_question,
                agent_id=prompt.agent_id
            ))
            return {"response": company_question, "history": [{
                "id": row["id"],
                "role": row["role"],
                "message": row["message"],
                "agent_id": row["agent_id"],
                "timestamp": row["timestamp"].isoformat()
            } for row in rows], "knowledge": await get_knowledge(prompt.conversation_id)}
        else:
            retry_message = random.choice(name_retry_phrases)
            await database.execute(chats.insert().values(
                conversation_id=prompt.conversation_id,
                role=agent_role,
                message=retry_message,
                agent_id=prompt.agent_id
            ))
            return {"response": retry_message, "history": [{
                "id": row["id"],
                "role": row["role"],
                "message": row["message"],
                "agent_id": row["agent_id"],
                "timestamp": row["timestamp"].isoformat()
            } for row in rows], "knowledge": knowledge_data}

    # Ask for Company (onboarding)
    if "company" not in knowledge_data:
        company_extracted = await extract_name_with_validator(prompt.user_input, "company's name")
        if company_extracted:
            await save_knowledge(prompt.conversation_id, "company", company_extracted)
        else:
            company_retry_phrases = [
                "I didn't catch your company's name. Could you please repeat it?",
                "Sorry, I missed your company name. Can you share it again?",
                "Oops, I didn't get your company. What's your company's name?",
                "Would you mind telling me your company name one more time?",
                "Could you please clarify your company name for me?",
                "I'm having trouble hearing your company's name. Could you share it again?",
                "Sorry, I couldn't understand your company name. Can you write it again?",
                "Just to confirm, what's your company's name?",
                "Apologies, I didn't catch your company name. Can you let me know again?",
            ]
            retry_message = random.choice(company_retry_phrases)

            await database.execute(chats.insert().values(
                conversation_id=prompt.conversation_id,
                role=agent_role,
                message=retry_message,
                agent_id=prompt.agent_id
            ))
            return {"response": retry_message, "history": [{
                "id": row["id"],
                "role": row["role"],
                "message": row["message"],
                "agent_id": row["agent_id"],
                "timestamp": row["timestamp"].isoformat()
            } for row in rows], "knowledge": knowledge_data}

    history_text = format_history(rows)
    full_prompt = f"{agent_definition}\nContext:\n{history_text}\nUser: {prompt.user_input}\nAgent:"

    print(f"[LLM PROMPT]\n{full_prompt}")

    response = llm(full_prompt, max_tokens=650, temperature=0.5, stop=["User:", "\n"])
    response_text = response['choices'][0]['text'].strip()

    print(f"[LLM RESPONSE]\n{response_text}")

    if not response_text:
        fallback_phrases = [
            "Sorry, I didn't understand that. Could you please rephrase?",
            "Hmm, I didn't quite catch that. Could you say it again another way?",
            "Apologies, could you rephrase your message?",
            "I’m not sure I understood. Can you try explaining it differently?",
            "Could you clarify that for me?",
            "Sorry, I missed that. Can you tell me again?",
            "I didn’t get that. Would you mind rephrasing?",
            "Could you please say that again in another way?",
            "I’m having trouble understanding. Can you explain that again?",
        ]
        fallback = random.choice(fallback_phrases)

        print("[LLM] Empty response detected, using fallback.")
        await database.execute(chats.insert().values(
            conversation_id=prompt.conversation_id,
            role=agent_role,
            message=fallback,
            agent_id=prompt.agent_id
        ))
        return {
            "response": fallback,
            "history": [{
                "id": row["id"],
                "role": row["role"],
                "message": row["message"],
                "agent_id": row["agent_id"],
                "timestamp": row["timestamp"].isoformat()
            } for row in rows],
            "knowledge": await get_knowledge(prompt.conversation_id)
        }
    else:
        await database.execute(chats.insert().values(
            conversation_id=prompt.conversation_id,
            role=agent_role,
            message=response_text,
            agent_id=prompt.agent_id
        ))
        return {
            "response": response_text,
            "history": [{
                "id": row["id"],
                "role": row["role"],
                "message": row["message"],
                "agent_id": row["agent_id"],
                "timestamp": row["timestamp"].isoformat()
            } for row in rows],
            "knowledge": await get_knowledge(prompt.conversation_id)
        }

# --- ENDPOINTS ---
@app.post("/generate_voice")
async def generate_voice_response(prompt: Prompt):
    agent = AGENTS.get(prompt.agent_id, AGENTS["max"])
    return await generate_response(prompt, "user voice", "agent voice", agent["name"], agent["definition"])

@app.post("/generate_text")
async def generate_text_response(prompt: Prompt):
    agent = AGENTS.get(prompt.agent_id, AGENTS["max"])
    return await generate_response(prompt, "user text", "agent text", agent["name"], agent["definition"])
