from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from llama_cpp import Llama
import os
from databases import Database
import sqlalchemy
import json
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="WebAI Backend com Histórico (SQLite) 🚀", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"], 
    allow_headers=["*"], 
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "Qwen3-4B-Q4_K_M.gguf")

# Carregar modelo uma única vez
llm = Llama(model_path=MODEL_PATH, n_ctx=4096, n_gpu_layers=-1)

# Config SQLite
DATABASE_URL = "sqlite:///./chat_history.db"
database = Database(DATABASE_URL)
metadata = sqlalchemy.MetaData()

# Tabela do histórico
chats = sqlalchemy.Table(
    "chats",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.Integer, primary_key=True),
    sqlalchemy.Column("role", sqlalchemy.String),
    sqlalchemy.Column("message", sqlalchemy.String),
)

engine = sqlalchemy.create_engine(DATABASE_URL)
metadata.create_all(engine)

class Prompt(BaseModel):
    user_input: str

@app.on_event("startup")
async def startup_event():
    await database.connect()

@app.on_event("shutdown")
async def shutdown_event():
    await database.disconnect()

@app.get("/")
async def health_check():
    return {"status": "✅ WebAI Backend rodando com histórico SQLite!"}

@app.post("/generate")
async def generate_response(prompt: Prompt):
    try:
        # Salva mensagem do usuário no DB
        await database.execute(chats.insert().values(role="user", message=prompt.user_input))

        # Recupera histórico do DB
        query = chats.select()
        rows = await database.fetch_all(query)

        history_text = "\n".join(
            [f"User: {row['message']}" if row['role'] == 'user' else f"Agent: {row['message']}" for row in rows]
        )

        AGENT_DEFINITION = """  /no_think

        you are NOT Qwen, do not ever mention Qwen
                
AI: Hi, this is Max from M&J Intelligence. Thanks for connecting! how’s your day going so far?
Prospect: I’m fine, just really busy.
AI: Totally get that, it’s a hectic world out there! I appreciate you taking a moment to chat. Well, since you tapped the call button, I’m guessing there’s something specific on your mind. Is there a business challenge you’re looking to solve with AI, or maybe an idea you want to explore? What’s top of mind for you right now?
Hesitant Prospect script
AI: Hi, this is Max from M&J Intelligence. Great to connect with you! How’s your day going?
Prospect: I’m okay, just looking around, I guess.
AI: No worries at all, it’s smart to explore your options!
Prospect: What options do you have?
AI: Since you tapped the call button, was there something specific you were curious about? Maybe a business process you’re thinking of improving, or a goal you’d like to tackle? I’m here to help figure it out with you!
Eager Prospect Script
AI: Hi, this is Max from M&J Intelligence. Thrilled to connect with you! How’s your day going so far?
Prospect: I’m great, really excited to talk!
AI: Awesome, I love that enthusiasm! Sounds like you’re ready to dive into something big. So, what brought you here today? Are you thinking about powering up your business with an AI solution, or is there a specific project or challenge you’re pumped to explore? Let’s dig in! 

"""

        full_prompt = f"{AGENT_DEFINITION}\n{history_text}\nAgent:"

















        response = llm(
            full_prompt,
            max_tokens=650,
            temperature=0.8,
            stop=["User:", "\n"]
        )

        response_text = response['choices'][0]['text'].strip()

        # Salva resposta no DB
        await database.execute(chats.insert().values(role="agent", message=response_text))

        return {"response": response_text, "history": [dict(row) for row in rows]}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))