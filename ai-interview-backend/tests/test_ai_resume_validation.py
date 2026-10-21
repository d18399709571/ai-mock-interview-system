import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from app.services.client.ai_service import AIService


def test_parse_resume_accepts_valid_structured_output():
    response = (
        '{"name":"张三","education":"本科","skills":["Python"],'
        '"experience":["实习经历"],"projects":["项目A"],'
        '"summary":"电子信息工程学生"}'
    )

    with patch.object(AIService, "_chat", new=AsyncMock(return_value=response)):
        result = asyncio.run(AIService.parse_resume("原始简历文本"))

    assert result["name"] == "张三"
    assert result["skills"] == ["Python"]


def test_parse_resume_rejects_missing_required_fields():
    response = '{"name":"张三","skills":["Python"]}'

    with patch.object(AIService, "_chat", new=AsyncMock(return_value=response)):
        with pytest.raises(ValidationError):
            asyncio.run(AIService.parse_resume("原始简历文本"))


def test_parse_resume_rejects_wrong_field_type():
    response = (
        '{"name":"张三","education":"本科","skills":"Python",'
        '"experience":[],"projects":[],"summary":"学生"}'
    )

    with patch.object(AIService, "_chat", new=AsyncMock(return_value=response)):
        with pytest.raises(ValidationError):
            asyncio.run(AIService.parse_resume("原始简历文本"))


def test_analyze_resume_rejects_score_out_of_range():
    response = (
        '{"overall_score":11,"strengths":[],"weaknesses":[],'
        '"suggestions":[],"keyword_match":[],"missing_keywords":[],'
        '"summary":"分析完成"}'
    )

    with patch.object(AIService, "_chat", new=AsyncMock(return_value=response)):
        with pytest.raises(ValidationError):
            asyncio.run(AIService.analyze_resume({"name":"张三"}, "后端实习"))
