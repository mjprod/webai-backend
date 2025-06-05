from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from llama_cpp import Llama
import os
from databases import Database
import sqlalchemy
import json
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func
from pydantic import BaseModel, Field
from .data.agents import AGENTS

app = FastAPI(title="WebAI Backend with History (SQLite) 🚀", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"], 
    allow_headers=["*"], 
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "gemma-3-12b-it-Q4_K_S.gguf")

llm = Llama(model_path=MODEL_PATH, n_ctx=4096, n_gpu_layers=-1)

DATABASE_URL = "sqlite:///./chat_history.db"
database = Database(DATABASE_URL)
metadata = sqlalchemy.MetaData()

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

engine = sqlalchemy.create_engine(DATABASE_URL)
metadata.create_all(engine)

# Updated: Require conversation_id
class Prompt(BaseModel):
    """
    Model for user input and conversation identification.
    """
    user_input: str = Field(..., description="The message from the user.", example="Hi, can you help me?")
    conversation_id: str = Field(
        ..., 
        min_length=1, 
        description="Unique identifier for the conversation. Required and must not be empty.",
        example="001"
    )
    agent_id: str = Field("max", description="Agent identifier.", example="max")  

@app.on_event("startup")
async def startup_event():
    await database.connect()

@app.on_event("shutdown")
async def shutdown_event():
    await database.disconnect()

@app.get("/")
async def health_check():
    return {"status": "✅ WebAI Backend running with SQLite history!"}

def format_history(rows, last_n=5):
    # Ordena pelo timestamp caso venha bagunçado
    rows = sorted(rows, key=lambda x: x["timestamp"])
    # Pega só as últimas N mensagens
    recent_rows = rows[-last_n:]
    history = []
    for row in recent_rows:
        if row["role"].startswith("user"):
            history.append(f"User: {row['message']}")
        else:
            history.append(f"Agent: {row['message']}")
    return "\n".join(history)

async def generate_response(prompt: Prompt, user_role: str, agent_role: str, agent_name: str, agent_definition: str):
    try:
        if not prompt.conversation_id:
            raise HTTPException(status_code=400, detail="conversation_id is required")

        await database.execute(chats.insert().values(
            conversation_id=prompt.conversation_id,
            role=user_role,
            message=prompt.user_input,
            agent_id=prompt.agent_id 
        ))

        query = chats.select().where(chats.c.conversation_id == prompt.conversation_id)
        rows = await database.fetch_all(query)

        # Formata histórico para o prompt
        history_text = format_history(rows)

        # Monta system prompt
        AGENT_DEFINITION = agent_definition
        full_prompt = f"{AGENT_DEFINITION}\nContext:\n{history_text}\nUser: {prompt.user_input}\nAgent:"

        # Chama o modelo
        response = llm(
            full_prompt,
            max_tokens=650,
            temperature=0.8,
            stop=["User:", "\n"]
        )
        response_text = response['choices'][0]['text'].strip()

        # Salva resposta da IA
        await database.execute(chats.insert().values(
            conversation_id=prompt.conversation_id,
            role=agent_role,
            message=response_text,
            agent_id=prompt.agent_id 
        ))

        return {
            "response": response_text,
            "history": [
                {
                    "id": row["id"],
                    "role": row["role"],
                    "message": row["message"],
                    "agent_id": row["agent_id"],
                    "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None
                }
                for row in rows
            ]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Agora, os endpoints só chamam essa função, mudando o nome/role
@app.post("/generate_voice")
async def generate_voice_response(prompt: Prompt):
    agent = AGENTS.get(prompt.agent_id, AGENTS["max"])
    agent_definition = agent["definition"]
    agent_name = agent["name"]
    return await generate_response(
        prompt,
        user_role="user voice",
        agent_role="agent voice",
        agent_name=agent_name,
        agent_definition=agent_definition
    )

@app.post("/generate_text")
async def generate_text_response(prompt: Prompt):
    agent = AGENTS.get(prompt.agent_id, AGENTS["max"])
    agent_definition = agent["definition"]
    agent_name = agent["name"]
    return await generate_response(
        prompt,
        user_role="user text",
        agent_role="agent text",
        agent_name=agent_name,
        agent_definition=agent_definition 
    )