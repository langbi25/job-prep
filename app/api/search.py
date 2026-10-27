from fastapi import APIRouter
from pydantic import BaseModel

from ..services import rag_service

router = APIRouter(prefix="/api", tags=["search"])


class SearchBody(BaseModel):
    query: str
    scope: str = "cards"       # cards / questions
    top_k: int = 5
    mode: str = "hybrid"       # fts / hybrid / full


@router.get("/search")
def search_get(query: str, scope: str = "cards", top_k: int = 5, mode: str = "hybrid"):
    return rag_service.search(query, scope=scope, top_k=top_k, mode=mode)


@router.post("/search")
def search_post(body: SearchBody):
    return rag_service.search(body.query, scope=body.scope, top_k=body.top_k, mode=body.mode)
