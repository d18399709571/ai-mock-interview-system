"""Explicit, offline demo entry point. Production main.py never imports this."""
import asyncio
import json
import os
from pathlib import Path

if os.environ.get("INTERVIEW_DEMO") != "synthetic-only":
    raise RuntimeError("Use the isolated demo Compose file; synthetic-only opt-in required")

from app.services.client.ai_service import AIService
from app.schedule.jobs.email_tasks import send_verification_email_task

OUTBOX = Path("/runtime/outbox.json")

def capture_mail(email, code, code_type, first_name=None):
    # Local synthetic mail sink; no SMTP/Brevo/Celery worker is used.
    data = json.loads(OUTBOX.read_text()) if OUTBOX.exists() else {}
    data[email] = {"code": code, "type": code_type}
    OUTBOX.write_text(json.dumps(data), encoding="utf-8")

async def parse_resume(text):
    return {"name": "合成演示候选人", "skills": ["Python", "FastAPI"], "demo": True}

async def analyze_resume(parsed, position):
    return {"strengths": ["演示数据：掌握基础语法"], "weaknesses": ["演示数据：需要项目练习"], "suggestions": ["练习事务与并发"]}

async def questions(parsed_resume, target_position, difficulty, count):
    return [{"question": f"合成演示第 {i+1} 题：请说明一个 Python 后端实践。"} for i in range(count)]

async def evaluate(**kwargs):
    await asyncio.sleep(0.15)
    return {"score": 7.5, "feedback": "演示专用固定评分：建议结合项目实例回答。"}

async def stream(**kwargs):
    result = await evaluate(**kwargs)
    yield result["feedback"] + "\n"
    yield json.dumps({"score": result["score"]})

async def report(**kwargs):
    return {"summary": "演示专用确定性报告，不代表真实模型能力。", "strengths": ["合成优势"],
            "weaknesses": ["合成改进项"], "suggestions": ["练习项目讲解"], "hire_recommendation": "演示数据，无录用结论"}

send_verification_email_task.delay = capture_mail
AIService.parse_resume = staticmethod(parse_resume)
AIService.analyze_resume = staticmethod(analyze_resume)
AIService.generate_questions = staticmethod(questions)
AIService.evaluate_answer = staticmethod(evaluate)
AIService.evaluate_answer_stream = staticmethod(stream)
AIService.generate_report = staticmethod(report)

from main import app

@app.get("/demo-status", include_in_schema=False)
async def demo_status():
    return {"demo": "synthetic-only", "ai": "deterministic substitute", "mail": "local outbox"}
