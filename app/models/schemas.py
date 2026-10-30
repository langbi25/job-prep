from pydantic import BaseModel, Field


# ---------- 知识卡片 ----------
class CardCreate(BaseModel):
    title: str
    domain: str = "backend"
    topic: str = "通用"
    question: str = ""
    points: list[str] = Field(default_factory=list)
    pitfalls: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_type: str = "manual"


class CardUpdate(BaseModel):
    title: str | None = None
    domain: str | None = None
    topic: str | None = None
    question: str | None = None
    points: list[str] | None = None
    pitfalls: list[str] | None = None
    tags: list[str] | None = None


# ---------- 采集 ----------
class IngestCreate(BaseModel):
    source_type: str = "paste"      # paste / url / bookmarklet / file
    text: str = ""                  # 粘贴的正文（通道 A）
    url: str = ""                   # 来源链接（通道 B/C 时后端抓取正文）
    selection: str = ""             # bookmarklet 带来的选中文字


class ConfirmedQuestion(BaseModel):
    keep: bool = True
    text: str
    qtype: str = ""
    topic: str = ""
    merge_into: str | None = None   # 人工指定的归并目标 q-xxxx


class ConfirmPayload(BaseModel):
    company: str = ""
    position: str = ""
    direction: str = ""          # 空 = 回落到抽取结果的 direction
    round: str = ""
    interview_date: str = ""
    result: str = "unknown"
    questions: list[ConfirmedQuestion] = Field(default_factory=list)  # 空 = 回落到抽取结果的全部题目


class MergePayload(BaseModel):
    into: str
