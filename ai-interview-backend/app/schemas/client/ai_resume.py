from pydantic import BaseModel, Field


class ResumeParsedOutput(BaseModel):
    """AI 简历结构化解析结果。"""

    name: str
    education: str
    skills: list[str]
    experience: list[str]
    projects: list[str]
    summary: str


class ResumeAnalysisOutput(BaseModel):
    """AI 简历岗位分析结果。"""

    overall_score: float = Field(ge=1, le=10)
    strengths: list[str]
    weaknesses: list[str]
    suggestions: list[str]
    keyword_match: list[str]
    missing_keywords: list[str]
    summary: str
