"""Wire HTTP demo acceptance. Runs INSIDE the isolated API container.

Never prints passwords, verification codes or tokens. Uses synthetic users only.
"""
import asyncio
import json
import os
from pathlib import Path
import uuid
import httpx
from app.db.base import get_session_local, close_db_engine
from app.models.admin import Admin

def pdf():
    stream = b"BT /F1 12 Tf 40 760 Td (Synthetic Candidate - Python FastAPI project) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    data = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(f"{n:010d} 00000 n \n".encode() for n in offsets)
    return data + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()

async def main():
    password = os.environ["DEMO_PASSWORD"]
    suffix = uuid.uuid4().hex[:12]
    email = f"synthetic-{suffix}@example.com"
    admin_email = f"synthetic-admin-{suffix}@example.com"
    Path("/runtime/demo-accounts.json").write_text(json.dumps({"user": email, "admin": admin_email}), encoding="utf-8")
    async with get_session_local()() as db:
        db.add(Admin(email=admin_email, password=Admin.get_password_hash(password), role="admin", is_active=True))
        await db.commit()
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8001", timeout=20) as client:
        async def request(method, path, expected=200, **kwargs):
            response = await client.request(method,path,**kwargs)
            print(json.dumps({"method":method,"path":path,"status":response.status_code}), flush=True)
            assert response.status_code == expected, response.text[:500]
            return response.json().get("data") if response.headers.get("content-type", "").startswith("application/json") else response.text
        await request("GET","/demo-status")
        await request("GET","/api/v1/config/health")
        await request("POST","/api/v1/auth/register",json={"email":email,"password":password,"first_name":"Synthetic","last_name":"Demo"})
        outbox = json.loads(Path("/runtime/outbox.json").read_text())
        verified = await request("POST","/api/v1/auth/verify-email",json={"email":email,"code":outbox[email]["code"]})
        assert verified["user"]["is_verified"] is True
        login = await request("POST","/api/v1/auth/login",json={"email":email,"password":password})
        headers = {"Authorization":"Bearer " + login["access_token"]}
        other_email = f"synthetic-other-{suffix}@example.com"
        await request("POST","/api/v1/auth/register",json={"email":other_email,"password":password,"first_name":"Other","last_name":"Demo"})
        outbox = json.loads(Path("/runtime/outbox.json").read_text())
        other = await request("POST","/api/v1/auth/verify-email",json={"email":other_email,"code":outbox[other_email]["code"]})
        other_headers = {"Authorization":"Bearer " + other["access_token"]}
        resume = await request("POST","/api/v1/resumes/upload",headers=headers,
                               files={"file":("synthetic.pdf",pdf(),"application/pdf")}, data={"target_position":"Python backend demo"})
        for total in (3,5):
            interview = await request("POST","/api/v1/interviews/start",headers=headers,json={"resume_id":resume["resume_id"],"total_questions":total})
            iid = interview["interview_id"]
            payload={"answer":"synthetic unauthorized", "expected_question_index":0}
            for route in ("answer","answer/stream"):
                await request("POST",f"/api/v1/interviews/{iid}/{route}",expected=401,json=payload)
            await request("POST",f"/api/v1/interviews/{iid}/answer",expected=404,headers=other_headers,json=payload)
            denied = await request("POST",f"/api/v1/interviews/{iid}/answer/stream",headers=other_headers,json=payload)
            denial=json.loads(next(line[5:] for line in denied.splitlines() if line.startswith("data:")))
            assert denial["type"]=="error" and denial["code"]=="answer_rejected" and denial["retry_safe"] is False
            # Missing/invalid identity must be rejected before any answer processing.
            for value in (None,-1,True,"0"):
                payload = {"answer":"synthetic"}
                if value is not None:
                    payload["expected_question_index"] = value
                for route in ("answer","answer/stream"):
                    await request("POST",f"/api/v1/interviews/{iid}/{route}",expected=400,headers=headers,json=payload)
            for index in range(total):
                result = await request("POST",f"/api/v1/interviews/{iid}/answer/stream",headers=headers,
                                       json={"answer":f"Synthetic answer {index}","expected_question_index":index})
                events = [json.loads(line[5:]) for line in result.splitlines() if line.startswith("data:")]
                assert events[-1]["type"] == "done" and events[-1]["question_index"] == index
                assert events[-1]["is_finished"] is (index == total-1)
                await request("POST",f"/api/v1/interviews/{iid}/answer",expected=409,headers=headers,
                              json={"answer":"late old answer", "expected_question_index":index})
            messages = await request("GET",f"/api/v1/interviews/{iid}/messages",headers=headers)
            assert len(messages) == total*2
            report = await request("GET",f"/api/v1/interviews/{iid}/report",headers=headers)
            assert report["overall_score"] == 7.5 and len(report["report"]["question_scores"]) == total
            admin = await request("POST","/api/v1/backoffice/auth/login",json={"email":admin_email,"password":password})
            detail = await request("GET",f"/api/v1/backoffice/interviews/{iid}",headers={"Authorization":"Bearer " + admin["access_token"]})
            assert detail["status"] == "completed" and len(detail["messages"]) == total*2
            print(json.dumps({"PASS":f"wire-demo-{total}","interview_id":iid,"messages":len(messages),"overall":report["overall_score"]}),flush=True)
    await close_db_engine()

if __name__ == "__main__":
    asyncio.run(main())
