"""Actual interview methods, AI substitute and in-memory transaction model."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from _a2_test_support import (
    APIException, FakeDB, Interview, InterviewMessage, IsolatedCase,
    Resume, namespace, source_class,
)


class InterviewScoreTests(IsolatedCase):
    def fixture(self, score=7, text=None, fail_model=False, fail_report=False, last=False):
        interview = Interview(id=1, user_id=17)
        interview.resume_id = 2
        interview.status = "in_progress"
        interview.current_question_index = 0
        interview.total_questions = 1 if last else 2
        interview.questions_data = [{"question": "Q1"}, {"question": "Q2"}]
        interview.target_position = "Synthetic developer"
        interview.report = None
        interview.overall_score = None
        resume = Resume(id=2)
        resume.parsed_content = "{}"
        first = InterviewMessage(id=3, interview_id=1, role="interviewer", content="Q1",
                                 question_index=0, score=None)
        db = FakeDB(interview, resume, first)

        async def stream(**kwargs):
            yield "Synthetic feedback\n"
            if fail_model:
                raise RuntimeError("synthetic provider failure")
            yield text if text is not None else json.dumps({"score": score})

        ai = SimpleNamespace(
            evaluate_answer=AsyncMock(side_effect=RuntimeError("synthetic failure"))
            if fail_model else AsyncMock(return_value={"score": score, "feedback": "synthetic"}),
            evaluate_answer_stream=stream,
            generate_report=AsyncMock(side_effect=RuntimeError("synthetic report failure"))
            if fail_report else AsyncMock(return_value={"summary": "Synthetic report"}))
        env = namespace()
        env["AIService"] = ai
        service = source_class("app/services/client/interview_service.py", "InterviewService", env)
        return service, db, interview, ai

    def events(self, service, db):
        async def collect():
            return [json.loads(event.removeprefix("data: "))
                    async for event in service.submit_answer_stream(db, 17, 1, "Synthetic answer")]
        return self.run_async(collect())

    def assert_rolled_back(self, db, interview):
        self.assertEqual(db.commits, 0)
        self.assertGreaterEqual(db.rollbacks, 1)
        self.assertEqual(db.pending, [])
        self.assertEqual(interview.current_question_index, 0)
        self.assertEqual(interview.status, "in_progress")
        self.assertIsNone(interview.report)
        self.assertFalse(any(r.role == "candidate" for r in db.rows if isinstance(r, InterviewMessage)))

    def test_normal_and_stream_reject_invalid_scores_without_advancing(self):
        for value in (None, "7", True, {}, [], float("nan"), float("inf"),
                      -float("inf"), -1, 10.1):
            for streaming in (False, True):
                with self.subTest(value_type=type(value).__name__, streaming=streaming):
                    service, db, interview, _ = self.fixture(score=value)
                    if streaming:
                        events = self.events(service, db)
                        self.assertEqual(events[-1]["type"], "error")
                        self.assertTrue(events[-1]["retry_safe"])
                        self.assertNotIn("done", [e["type"] for e in events])
                    else:
                        with self.assertRaises(APIException):
                            self.run_async(service.submit_answer(db, 17, 1, "Synthetic answer"))
                    self.assert_rolled_back(db, interview)

    def test_stream_missing_malformed_and_multiple_scores_fail(self):
        for text in ("", "feedback only", '{"score":}', '{"score":7}{"score":8}'):
            service, db, interview, _ = self.fixture(text=text)
            events = self.events(service, db)
            self.assertEqual(events[-1]["type"], "error")
            self.assertNotIn("done", [e["type"] for e in events])
            self.assert_rolled_back(db, interview)

    def test_missing_normal_score_does_not_default_to_five(self):
        service, db, interview, ai = self.fixture()
        ai.evaluate_answer.return_value = {"feedback": "No score"}
        with self.assertRaises(APIException):
            self.run_async(service.submit_answer(db, 17, 1, "Synthetic answer"))
        self.assert_rolled_back(db, interview)

    def test_provider_failure_rolls_back_both_methods(self):
        for streaming in (False, True):
            service, db, interview, _ = self.fixture(fail_model=True)
            if streaming:
                self.assertEqual(self.events(service, db)[-1]["type"], "error")
            else:
                with self.assertRaises(RuntimeError):
                    self.run_async(service.submit_answer(db, 17, 1, "Synthetic answer"))
            self.assert_rolled_back(db, interview)

    def test_report_failure_cannot_commit_completed_interview(self):
        for streaming in (False, True):
            service, db, interview, _ = self.fixture(fail_report=True, last=True)
            if streaming:
                self.assertEqual(self.events(service, db)[-1]["type"], "error")
            else:
                with self.assertRaises(RuntimeError):
                    self.run_async(service.submit_answer(db, 17, 1, "Synthetic answer"))
            self.assert_rolled_back(db, interview)

    def test_valid_zero_ten_and_normal_scores_commit(self):
        for value in (0, 7.5, 10):
            for streaming in (False, True):
                service, db, interview, _ = self.fixture(score=value)
                result = self.events(service, db)[-1] if streaming else self.run_async(
                    service.submit_answer(db, 17, 1, "Synthetic answer"))
                self.assertEqual(result["score"], value)
                self.assertEqual(db.commits, 1)
                self.assertEqual(db.rollbacks, 0)
                self.assertEqual(interview.current_question_index, 1)

    def test_final_report_success_commits_before_done(self):
        service, db, interview, _ = self.fixture(last=True)
        events = self.events(service, db)
        self.assertEqual(events[-1]["type"], "done")
        self.assertTrue(events[-1]["is_finished"])
        self.assertEqual(interview.status, "completed")
        self.assertEqual(json.loads(interview.report)["summary"], "Synthetic report")
        self.assertEqual(db.commits, 1)

    def test_disconnect_before_commit_rolls_back(self):
        service, db, interview, _ = self.fixture()

        async def disconnect():
            generator = service.submit_answer_stream(db, 17, 1, "Synthetic answer")
            await anext(generator)
            await generator.aclose()

        self.run_async(disconnect())
        self.assert_rolled_back(db, interview)

    def test_disconnect_after_commit_does_not_claim_rollback(self):
        service, db, interview, _ = self.fixture()

        async def disconnect():
            generator = service.submit_answer_stream(db, 17, 1, "Synthetic answer")
            async for event in generator:
                if json.loads(event.removeprefix("data: "))["type"] == "done":
                    break
            await generator.aclose()

        self.run_async(disconnect())
        self.assertEqual(db.commits, 1)
        self.assertEqual(db.rollbacks, 0)
        self.assertEqual(interview.current_question_index, 1)

    def test_commit_acknowledgement_loss_is_not_safe_to_repeat(self):
        service, db, interview, _ = self.fixture()
        commit = db.commit

        async def lost_ack():
            await commit()
            raise OSError("synthetic lost acknowledgement")

        db.commit = lost_ack
        events = self.events(service, db)
        self.assertEqual(events[-1]["type"], "error")
        self.assertFalse(events[-1]["retry_safe"])
        self.assertEqual(db.commits, 1)
        self.assertEqual(interview.current_question_index, 1)
