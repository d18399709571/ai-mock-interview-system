"""Explicit opt-in PostgreSQL acceptance checks; deterministic AI, real sessions.

Run: python tests/test_pg_answer_concurrency.py [--baseline]
Uses the configured disposable database. Creates synthetic rows; never deletes data.
"""
import asyncio
import json
import logging
import sys
import socket
import time
import uuid
from collections import Counter
from unittest.mock import patch

from sqlalchemy import event, select, text
from app.db.base import get_engine, get_session_local
from app.models import User, Resume, Interview, InterviewMessage
from app.services.client.ai_service import AIService
from app.services.client.interview_service import InterviewService

BASELINE = "--baseline" in sys.argv
EVENTS = []
POOL = {"active": 0, "peak": 0}
guard_logger = logging.getLogger("app.services.client.interview_service")
guard_logger.setLevel(logging.INFO)
guard_logger.addHandler(logging.StreamHandler(sys.stdout))

def record(event, **data):
    row = dict(event=event, monotonic=time.monotonic(), **data)
    EVENTS.append(row)
    print(json.dumps(row, ensure_ascii=False, default=str), flush=True)

async def fixture(total=3):
    async with get_session_local()() as db:
        user = User(email=f"synthetic-{uuid.uuid4().hex}@example.com", is_active=True, is_verified=True)
        db.add(user)
        await db.flush()
        resume = Resume(user_id=user.id, parsed_content='{"name":"Synthetic Candidate"}', status="completed")
        db.add(resume)
        await db.flush()
        interview = Interview(user_id=user.id, resume_id=resume.id, status="in_progress",
                              total_questions=total, current_question_index=0,
                              target_position="Synthetic Python developer", difficulty="medium",
                              questions_data=[{"question": f"Synthetic Q{i+1}"} for i in range(total)])
        db.add(interview)
        await db.flush()
        db.add(InterviewMessage(interview_id=interview.id, role="interviewer", content="Synthetic Q1", question_index=0))
        await db.commit()
        return user.id, interview.id

async def snapshot(iid):
    async with get_session_local()() as db:
        interview = await db.get(Interview, iid)
        messages = (await db.execute(select(InterviewMessage).where(InterviewMessage.interview_id == iid).order_by(InterviewMessage.id))).scalars().all()
        rows = [dict(id=m.id, role=m.role, question_index=m.question_index, content=m.content,
                     score=float(m.score) if m.score is not None else None) for m in messages]
        data = dict(interview_id=iid, index=interview.current_question_index, status=interview.status,
                    overall=float(interview.overall_score) if interview.overall_score is not None else None,
                    report=json.loads(interview.report) if interview.report else None, messages=rows)
        record("database_snapshot", **data)
        return data

class ControlledAI:
    def __init__(self, blocked=False, score=7, fail=None):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()
        self.calls = 0
        self.reports = 0
        self.score = score
        self.fail = fail

    async def evaluate(self, **kwargs):
        self.calls += 1
        record("ai_enter", call=self.calls, answer=kwargs["answer"])
        self.entered.set()
        await self.release.wait()
        if self.fail == "evaluate":
            raise RuntimeError("synthetic evaluation failure")
        return dict(score=self.score, feedback="DEMO deterministic feedback")

    async def stream(self, **kwargs):
        value = await self.evaluate(**kwargs)
        yield "DEMO deterministic feedback\n"
        if self.fail == "stream":
            raise RuntimeError("synthetic stream interruption")
        yield json.dumps({"score": value["score"]})

    async def report(self, **kwargs):
        self.reports += 1
        record("report_enter", call=self.reports)
        if self.fail == "report":
            raise RuntimeError("synthetic report failure")
        return dict(summary="DEMO deterministic report", qa=kwargs["questions_and_scores"])

    def patches(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch.object(AIService, "evaluate_answer", self.evaluate))
        stack.enter_context(patch.object(AIService, "evaluate_answer_stream", self.stream))
        stack.enter_context(patch.object(AIService, "generate_report", self.report))
        return stack

async def invoke(kind, uid, iid, index=0, request_id="request", answer="Synthetic answer", db_hook=None):
    async with get_session_local()() as db:
        pid = (await db.execute(text("select pg_backend_pid()"))).scalar_one()
        await db.execute(text("SET LOCAL application_name = 'interview-v2-acceptance'"))
        record("request_start", request_id=request_id, kind=kind, expected_index=index, pid=pid)
        commit = db.commit
        async def logged_commit():
            record("commit_start", request_id=request_id, pid=pid)
            await commit()
            record("commit_complete", request_id=request_id, pid=pid)
        db.commit = logged_commit
        if db_hook:
            db_hook(db)
        kwargs = dict(db=db, user_id=uid, interview_id=iid, answer=answer)
        if not BASELINE:
            kwargs["expected_question_index"] = index
        try:
            if kind == "http":
                value = await InterviewService.submit_answer(**kwargs)
            else:
                value = [json.loads(e.removeprefix("data: ")) async for e in InterviewService.submit_answer_stream(**kwargs)]
            record("request_end", request_id=request_id, pid=pid, value=value)
            return value
        except Exception as exc:
            value = dict(error=type(exc).__name__, status=getattr(exc, "status_code", None))
            record("request_end", request_id=request_id, pid=pid, value=value)
            return value

def success(value):
    return (value[-1]["type"] == "done") if isinstance(value, list) else "error" not in value

def conflict(value):
    return (value[-1].get("code") == "answer_conflict") if isinstance(value, list) else value.get("status") == 409

def invariant(state, answered):
    counts = Counter((m["role"], m["question_index"]) for m in state["messages"])
    assert all(v == 1 for v in counts.values()), counts
    assert len([m for m in state["messages"] if m["role"] == "candidate"]) == answered
    assert state["index"] == answered, state

async def overlap(a, b, final=False):
    uid, iid = await fixture()
    index = 0
    if final:
        warm = ControlledAI()
        with warm.patches():
            for index in range(2):
                assert success(await invoke("http", uid, iid, index))
        index = 2
    ai = ControlledAI(blocked=True)
    begin = len(EVENTS)
    with ai.patches():
        first = asyncio.create_task(invoke(a, uid, iid, index, "A"))
        await asyncio.wait_for(ai.entered.wait(), 5)
        second = asyncio.create_task(invoke(b, uid, iid, index, "B"))
        if BASELINE:
            async with asyncio.timeout(5):
                while ai.calls < 2:
                    await asyncio.sleep(0.01)
        else:
            second_value = await asyncio.wait_for(asyncio.shield(second), 5)
            assert conflict(second_value), second_value
        async with get_session_local()() as observer:
            active = (await observer.execute(text("SELECT pid, state, wait_event_type, xact_start::text FROM pg_stat_activity WHERE datname = current_database() AND application_name = 'interview-v2-acceptance'"))).mappings().all()
            record("overlap_barrier", a_pending=not first.done(), b_pending=not second.done(), sessions=[dict(r) for r in active])
        assert not first.done()
        ai.release.set()
        values = await asyncio.wait_for(asyncio.gather(first, second), 10)
    rows = EVENTS[begin:]
    starts = [r for r in rows if r["event"] == "request_start"]
    ends = [r for r in rows if r["event"] == "request_end"]
    assert len({r["pid"] for r in starts}) == 2
    assert max(r["monotonic"] for r in starts) < min(r["monotonic"] for r in ends)
    state = await snapshot(iid)
    if BASELINE:
        assert all(success(v) for v in values)
        assert Counter((m["role"],m["question_index"]) for m in state["messages"])[("candidate",index)] == 2
        record("VULNERABILITY_REPRODUCED", paths=[a,b], iid=iid)
    else:
        assert sum(success(v) for v in values) == 1
        assert ai.calls == 1
        invariant(state, index+1)
        if final:
            assert ai.reports == 1 and state["status"] == "completed"
        record("PASS", scenario=f"overlap-{a}-{b}-final={final}")

async def late_retry():
    uid, iid = await fixture()
    ai = ControlledAI()
    with ai.patches():
        assert success(await invoke("http", uid, iid, 0))
        value = await invoke("sse", uid, iid, 0, "late-first-question")
    state = await snapshot(iid)
    if BASELINE:
        assert success(value) and state["index"] == 2
        record("VULNERABILITY_REPRODUCED", scenario="late-first-answer-consumed-Q2")
    else:
        assert conflict(value)
        invariant(state, 1)
        record("PASS", scenario="late-retry")

async def main():
    # Application name is visible in PostgreSQL observer snapshots.
    engine = get_engine()
    @event.listens_for(engine.sync_engine, "checkout")
    def checkout(*args):
        POOL["active"] += 1
        POOL["peak"] = max(POOL["peak"], POOL["active"])
    @event.listens_for(engine.sync_engine, "checkin")
    def checkin(*args):
        POOL["active"] -= 1
    record("database_version", value=await version())
    for a,b in [("http","http"),("sse","sse"),("http","sse"),("sse","http")]:
        await overlap(a,b)
    await late_retry()
    if not BASELINE:
        await overlap("sse", "http", final=True)
        await regressions()
    await engine.dispose()
    assert POOL["active"] == 0, POOL
    record("connection_pool", **POOL)
    record("COMPLETE", baseline=BASELINE)

async def version():
    async with get_session_local()() as db:
        return (await db.execute(text("select version()"))).scalar_one()

async def regressions():
    for total in (3, 5):
        for kind in ("http", "sse"):
            uid, iid = await fixture(total)
            ai = ControlledAI()
            scores = [0,10,7,5,9][:total]
            with ai.patches():
                for index, score in enumerate(scores):
                    ai.score = score
                    assert success(await invoke(kind, uid, iid, index))
            state = await snapshot(iid)
            invariant(state, total)
            assert state["status"] == "completed"
            assert state["overall"] == round(sum(scores)/total,1)
            assert [q["score"] for q in state["report"]["question_scores"]] == scores
            assert len(state["messages"]) == total*2 and ai.reports == 1
            record("PASS", scenario=f"complete-{total}-{kind}")
    for kind in ("http", "sse"):
        for score in (None,"7",True,-1,10.1,float("nan"),float("inf")):
            uid, iid = await fixture()
            ai = ControlledAI(score=score)
            with ai.patches():
                assert not success(await invoke(kind,uid,iid))
            invariant(await snapshot(iid),0)
        record("PASS", scenario=f"invalid-scores-{kind}")
        for failure in ("evaluate", "report", "stream"):
            if kind == "http" and failure == "stream":
                continue
            uid, iid = await fixture(1 if failure == "report" else 3)
            ai = ControlledAI(fail=failure)
            with ai.patches():
                assert not success(await invoke(kind,uid,iid))
            invariant(await snapshot(iid),0)
            # Retrying the original expected index after rollback must work.
            ai.fail = None
            with ai.patches():
                assert success(await invoke(kind,uid,iid))
            record("PASS", scenario=f"rollback-retry-{kind}-{failure}")
        uid, iid = await fixture()
        other_uid, other_iid = await fixture()
        ai = ControlledAI()
        with ai.patches():
            value = await invoke(kind,other_uid,iid)
            assert not success(value)
            assert ai.calls == 0
            assert success(await invoke(kind,other_uid,other_iid))
        invariant(await snapshot(iid),0)
        record("PASS", scenario=f"ownership-{kind}")
        for failure_stage in ("before_commit", "during_commit", "after_commit"):
            uid,iid = await fixture()
            ai = ControlledAI()
            def hook(db):
                commit = db.commit
                async def fail():
                    if failure_stage == "after_commit":
                        await commit()
                        raise OSError("synthetic lost commit acknowledgement")
                    await db.execute(text("SELECT 1 / 0"))
                if failure_stage == "before_commit":
                    # Both routes explicitly flush on the last question, before
                    # setting commit_started; this is a provable SQL rollback.
                    db.flush = fail
                else:
                    db.commit = fail
            if failure_stage == "before_commit":
                # Exercise the explicit pre-commit flush in both routes.
                uid,iid = await fixture(1)
            with ai.patches():
                value = await invoke(kind,uid,iid,db_hook=hook)
                assert not success(value)
                if kind == "sse":
                    assert value[-1]["retry_safe"] is (failure_stage == "before_commit")
                state = await snapshot(iid)
                invariant(state, int(failure_stage == "after_commit"))
                retry = await invoke(kind,uid,iid)
                assert conflict(retry) if failure_stage == "after_commit" else success(retry)
            record("PASS", scenario=f"commit-failure-{kind}-{failure_stage}")
        uid,iid = await fixture()
        ai = ControlledAI(blocked=True)
        with ai.patches(), patch.object(InterviewService,"ANSWER_TIMEOUT_SECONDS",0.15):
            value = await invoke(kind,uid,iid)
            assert not success(value)
            if kind == "http":
                assert value["status"] == 504
        invariant(await snapshot(iid),0)
        ai.release.set()
        with ai.patches():
            assert success(await invoke(kind,uid,iid))
        record("PASS", scenario=f"timeout-release-{kind}")
        uid,iid = await fixture()
        ai = ControlledAI(blocked=True)
        with ai.patches():
            task = asyncio.create_task(invoke(kind,uid,iid))
            await asyncio.wait_for(ai.entered.wait(),5)
            task.cancel()
            try:
                await task
                raise AssertionError("cancellation lost")
            except asyncio.CancelledError:
                pass
        invariant(await snapshot(iid),0)
        ai.release.set()
        with ai.patches():
            assert success(await invoke(kind,uid,iid))
        record("PASS", scenario=f"cancel-release-{kind}")
    uid,iid = await fixture()
    ai = ControlledAI()
    with ai.patches():
        async with get_session_local()() as db:
            generator = InterviewService.submit_answer_stream(db,uid,iid,"Synthetic disconnect",0)
            first = json.loads((await anext(generator)).removeprefix("data: "))
            assert first["type"] == "chunk"
            await generator.aclose()
        invariant(await snapshot(iid),0)
        assert success(await invoke("sse",uid,iid))
    record("PASS", scenario="sse-close-before-commit")
    uid,iid = await fixture()
    with ai.patches():
        async with get_session_local()() as db:
            generator = InterviewService.submit_answer_stream(db,uid,iid,"Synthetic done lost",0)
            async for raw in generator:
                if json.loads(raw.removeprefix("data: "))["type"] == "done":
                    # Simulate the transport losing the done frame after commit.
                    await generator.aclose()
                    break
        state = await snapshot(iid)
        invariant(state,1)
        assert conflict(await invoke("http",uid,iid,0,answer="Synthetic done lost"))
    record("PASS", scenario="commit-before-done-lost")
    for kind in ("http","sse"):
        uid,iid = await fixture()
        child = await asyncio.create_subprocess_exec(sys.executable,"-B",__file__,"--crash-child",str(uid),str(iid),kind,
                                                    stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
        async with asyncio.timeout(10):
            while True:
                line = await child.stdout.readline()
                assert line, "child exited before acquiring lock"
                record("child_output", text=line.decode().strip())
                if '"event": "ai_enter"' in line.decode():
                    break
        child.kill()
        await child.wait()
        record("child_killed", process_id=child.pid, returncode=child.returncode)
        invariant(await snapshot(iid),0)
        with ControlledAI().patches():
            assert success(await invoke(kind,uid,iid))
        record("PASS", scenario=f"process-crash-release-{kind}")
    await wire_recovery()

async def wire_recovery():
    """Real localhost TCP, real JWT/routes, cancellation and a lost done frame."""
    import httpx
    import uvicorn
    from app.core.security import AuthBase
    from app.route.route import create_app
    app = create_app()
    dropped = []
    drop_path = None
    async def transport_fault(scope, receive, send):
        ended = False
        async def wrapped(message):
            nonlocal ended
            if ended:
                return
            if scope.get("path") == drop_path and b'"type": "done"' in message.get("body", b""):
                dropped.append(True)
                ended = True
                await send(dict(type="http.response.body",body=b"",more_body=False))
            else:
                await send(message)
        await app(scope,receive,wrapped)
    sock = socket.socket()
    sock.bind(("127.0.0.1",0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(transport_fault,host="127.0.0.1",port=port,lifespan="off",access_log=False,log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}",timeout=10) as client:
            uid,iid = await fixture()
            headers={"Authorization":"Bearer " + AuthBase.create_access_token(str(uid),scope="client")}
            ai=ControlledAI()
            disconnected=asyncio.Event()
            async def stalled_stream(**kwargs):
                await ai.evaluate(**kwargs)
                try:
                    yield "Synthetic first chunk"
                    await asyncio.Event().wait()
                finally:
                    disconnected.set()
            ai.stream=stalled_stream
            with ai.patches():
                async with client.stream("POST",f"/api/v1/interviews/{iid}/answer/stream",headers=headers,
                                         json={"answer":"Synthetic wire disconnect","expected_question_index":0}) as response:
                    assert response.status_code==200
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            assert json.loads(line[5:])["type"]=="chunk"
                            break
                await asyncio.wait_for(disconnected.wait(),5)
                # Wait for the real server session's cancellation rollback.
                async with asyncio.timeout(5):
                    while True:
                        async with get_session_local()() as db:
                            try:
                                await db.execute(select(Interview).where(Interview.id==iid).with_for_update(nowait=True))
                                await db.rollback()
                                break
                            except Exception as exc:
                                await db.rollback()
                                assert getattr(getattr(exc,"orig",None),"sqlstate",None)=="55P03"
                        await asyncio.sleep(0.02)
                invariant(await snapshot(iid),0)
                assert success(await invoke("http",uid,iid))
            record("PASS",scenario="wire-sse-client-disconnect-release")
            uid,iid=await fixture()
            headers={"Authorization":"Bearer " + AuthBase.create_access_token(str(uid),scope="client")}
            drop_path=f"/api/v1/interviews/{iid}/answer/stream"
            with ControlledAI().patches():
                response=await client.post(drop_path,headers=headers,json={"answer":"Synthetic lost wire done","expected_question_index":0})
                assert response.status_code==200 and dropped==[True]
                assert '"type": "done"' not in response.text
                invariant(await snapshot(iid),1)
                messages=await client.get(f"/api/v1/interviews/{iid}/messages",headers=headers)
                assert messages.status_code==200
                assert any(m["role"]=="candidate" and m["question_index"]==0 for m in messages.json()["data"])
                late=await client.post(f"/api/v1/interviews/{iid}/answer",headers=headers,json={"answer":"Synthetic lost wire done","expected_question_index":0})
                assert late.status_code==409
            record("PASS",scenario="wire-commit-before-done-lost")
            drop_path=None
            uid,iid=await fixture()
            headers={"Authorization":"Bearer " + AuthBase.create_access_token(str(uid),scope="client")}
            ai=ControlledAI(blocked=True)
            with ai.patches():
                first=asyncio.create_task(client.post(f"/api/v1/interviews/{iid}/answer",headers=headers,json={"answer":"Synthetic first","expected_question_index":0}))
                await asyncio.wait_for(ai.entered.wait(),5)
                second=await client.post(f"/api/v1/interviews/{iid}/answer/stream",headers=headers,json={"answer":"Synthetic second","expected_question_index":0})
                assert second.status_code==200
                error=json.loads(next(line[5:] for line in second.text.splitlines() if line.startswith("data:")))
                assert error["code"]=="answer_conflict" and error["retry_safe"] is False
                ai.release.set()
                assert (await first).status_code==200
            invariant(await snapshot(iid),1)
            record("PASS",scenario="wire-sse-conflict-event-after-http-open")
    finally:
        server.should_exit=True
        await asyncio.wait_for(task,10)
        sock.close()

if __name__ == "__main__":
    if "--crash-child" in sys.argv:
        async def child():
            with ControlledAI(blocked=True).patches():
                await invoke(sys.argv[-1],int(sys.argv[-3]),int(sys.argv[-2]))
        asyncio.run(child())
    else:
        asyncio.run(main())
