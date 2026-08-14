from pydantic import BaseModel
import uuid

class ChatRequest(BaseModel):
    query_id: uuid.UUID
    message: str

class ChatResponse(BaseModel):
    response: str
