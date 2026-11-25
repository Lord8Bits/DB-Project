from pydantic import BaseModel, Field

class CommentCreate(BaseModel):
    user_id: int
    author_name: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=10000)

class ReplyCreate(BaseModel):
    user_id: int
    author_name: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=5000)
    reply_to_id: str | None = None

class LikeCreate(BaseModel):
    account_id: int
