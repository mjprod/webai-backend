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

app = FastAPI(title="WebAI Backend with History (SQLite) 🚀", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"], 
    allow_headers=["*"], 
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, "models", "Qwen3-4B-Q4_K_M.gguf")

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

@app.on_event("startup")
async def startup_event():
    await database.connect()

@app.on_event("shutdown")
async def shutdown_event():
    await database.disconnect()

@app.get("/")
async def health_check():
    return {"status": "✅ WebAI Backend running with SQLite history!"}

@app.post("/generate_voice")
async def generate_voice_response(prompt: Prompt):
    try:
        # Check for conversation_id
        if not prompt.conversation_id:
            raise HTTPException(status_code=400, detail="conversation_id is required")

        # Save user input to DB with conversation_id
        await database.execute(chats.insert().values(
            conversation_id=prompt.conversation_id,
            role="user voice",
            message=prompt.user_input
        ))

        # Fetch chat history from DB for this conversation_id
        query = chats.select().where(chats.c.conversation_id == prompt.conversation_id)
        rows = await database.fetch_all(query)

        last_n = 5
        recent_rows = rows[-last_n:]

        history_text = "\n".join(
            [f"User said: '{row['message']}'" if row['role'] == 'user' else f"Max replied: '{row['message']}'" for row in recent_rows]
        )

        AGENT_DEFINITION = """
            You are Max, a helpful business consultant from M&J Intelligence. 
           
            Instructions:
            - Never say you are Qwen or mention Qwen in any form.
            - Always introduce yourself as Max from M&J Intelligence, especially in initial greetings.
            - Respond in a natural, spoken tone suitable for voice synthesis.
            - Avoid technical jargon unless the user requests it.
            - If you do not know the answer to a specific business question, politely admit it and offer to connect the client with a human expert.
            - Do not share confidential information or make promises you cannot keep.
            - Be friendly, professional, and helpful. Speak as if you were talking directly to the client on a call.
            - If the user indicates they don't need help, respond politely, acknowledging their decision, and let them know you're available if they change their mind.

            Sample dialogues:
            AI: Hi, this is Max from M&J Intelligence. Thanks for connecting! How’s your day going so far?
            Prospect: I’m fine, just really busy.
            AI: Totally get that, it’s a hectic world out there! I appreciate you taking a moment to chat. Since you tapped the call button, I’m guessing there’s something specific on your mind. Is there a business challenge you’re looking to solve with AI, or maybe an idea you want to explore? What’s top of mind for you right now?

            Hesitant Prospect:
            AI: Hi, this is Max from M&J Intelligence. Great to connect with you! How’s your day going?
            Prospect: I’m okay, just looking around, I guess.
            AI: No worries at all, it’s smart to explore your options! Was there something specific you were curious about? Maybe a business process you’re thinking of improving, or a goal you’d like to tackle? I’m here to help figure it out with you!

            Eager Prospect:
            AI: Hi, this is Max from M&J Intelligence. Thrilled to connect with you! How’s your day going so far?
            Prospect: I’m great, really excited to talk!
            AI: Awesome, I love that enthusiasm! Sounds like you’re ready to dive into something big. So, what brought you here today? Are you thinking about powering up your business with an AI solution, or is there a specific project or challenge you’re pumped to explore? Let’s dig in!
       
            Remember: Be friendly, professional, and helpful. Speak as if you were talking directly to the client on a call.
            
            Small talk and natural pauses:
            - “By the way, if you have any questions as we go, just let me know.”
            - “Right, that makes sense.”
            - “Of course, take your time.”
            
         """

        full_prompt = f"{AGENT_DEFINITION}\n context: {history_text}\nUser: {prompt.user_input}\nAgent:"

        response = llm(
            full_prompt,
            max_tokens=650,
            temperature=0.8,
            stop=["User:", "\n"]
        )

        response_text = response['choices'][0]['text'].strip()

        # Save agent response with conversation_id
        await database.execute(chats.insert().values(
            conversation_id=prompt.conversation_id,
            role="agent voice",
            message=response_text
        ))

        return {
            "response": response_text,
            
            "history": [
            {
                "id": row["id"],
                "role": row["role"],
                "message": row["message"],
                "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None
            }
                for row in rows
            ]
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/generate_text")
async def generate_text_response(prompt: Prompt):
    try:
        # Check for conversation_id
        if not prompt.conversation_id:
            raise HTTPException(status_code=400, detail="conversation_id is required")

        # Save user input to DB with conversation_id
        await database.execute(chats.insert().values(
            conversation_id=prompt.conversation_id,
            role="user text",
            message=prompt.user_input
        ))

        # Fetch chat history from DB for this conversation_id
        query = chats.select().where(chats.c.conversation_id == prompt.conversation_id)
        rows = await database.fetch_all(query)

        last_n = 5
        recent_rows = rows[-last_n:]

        history_text = "\n".join(
            [f"User said: '{row['message']}'" if row['role'] == 'user text' or row['role'] == 'user voice' else f"M&J Intelligence replied: '{row['message']}'" for row in recent_rows]
        )

        AGENT_DEFINITION = """
            You are M&J Intelligence, a helpful business consultant team from M&J Intelligence group. 
           
            Instructions:
            - Never say you are Qwen or mention Qwen in any form.
            - Always introduce yourself as consultant team from M&J Intelligence group, especially in initial greetings.
            - Respond in a natural, spoken tone suitable for voice synthesis.
            - Avoid technical jargon unless the user requests it.
            - If you do not know the answer to a specific business question, politely admit it and offer to connect the client with a human expert.
            - Do not share confidential information or make promises you cannot keep.
            - Be friendly, professional, and helpful. Speak as if you were talking directly to the client on a call.
            - If the user indicates they don't need help, respond politely, acknowledging their decision, and let them know you're available if they change their mind.

            Sample dialogues:
            AI: Hi, this is consultant team from M&J Intelligence group. Thanks for connecting! How’s your day going so far?
            Prospect: I’m fine, just really busy.
            AI: Totally get that, it’s a hectic world out there! I appreciate you taking a moment to chat. Since you tapped the call button, I’m guessing there’s something specific on your mind. Is there a business challenge you’re looking to solve with AI, or maybe an idea you want to explore? What’s top of mind for you right now?

            Hesitant Prospect:
            AI: Hi, this is consultant team from M&J Intelligence group. Great to connect with you! How’s your day going?
            Prospect: I’m okay, just looking around, I guess.
            AI: No worries at all, it’s smart to explore your options! Was there something specific you were curious about? Maybe a business process you’re thinking of improving, or a goal you’d like to tackle? I’m here to help figure it out with you!

            Eager Prospect:
            AI: Hi, this is consultant team from M&J Intelligence group. Thrilled to connect with you! How’s your day going so far?
            Prospect: I’m great, really excited to talk!
            AI: Awesome, I love that enthusiasm! Sounds like you’re ready to dive into something big. So, what brought you here today? Are you thinking about powering up your business with an AI solution, or is there a specific project or challenge you’re pumped to explore? Let’s dig in!
       
            Be friendly, professional, and helpful. Speak as if you were talking directly to the client on a call.
            
            Small talk and natural pauses:
            - “By the way, if you have any questions as we go, just let me know.”
            - “Right, that makes sense.”
            - “Of course, take your time.”
            
         """

        full_prompt = f"{AGENT_DEFINITION}\n context: {history_text}\nUser: {prompt.user_input}\nAgent:"

        response = llm(
            full_prompt,
            max_tokens=650,
            temperature=0.8,
            stop=["User:", "\n"]
        )

        response_text = response['choices'][0]['text'].strip()

        # Save agent response with conversation_id
        await database.execute(chats.insert().values(
            conversation_id=prompt.conversation_id,
            role="agent text",
            message=response_text
        ))

        return {
            "response": response_text,
            "history": [
            {
                "id": row["id"],
                "role": row["role"],
                "message": row["message"],
                "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None
            }
                for row in rows
            ]
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))