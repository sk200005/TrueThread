from pydantic import BaseModel
import uuid

class ChatRequest(BaseModel):
    query_id: uuid.UUID
    message: str

class ChatResponse(BaseModel):
    response: str
    contexts: list[str] = []

# This class is a data model whose fields,
#  types, and values should be validated by Pydantic.