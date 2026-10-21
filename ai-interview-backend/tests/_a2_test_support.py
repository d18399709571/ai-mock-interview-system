"""Isolated method tests: real source/SQL expressions, in-memory ORM substitute."""
import ast
import asyncio
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta, UTC
import hashlib
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, select, update
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql.dml import Update

ROOT = Path(__file__).resolve().parents[1]
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String)
    is_active = Column(Boolean)
    is_verified = Column(Boolean)

    def verify_password(self, password):
        return password == "synthetic-password"


class Admin(User):
    __tablename__ = "admins"
    __mapper_args__ = {"concrete": True}
    id = Column(Integer, primary_key=True)
    email = Column(String)
    is_active = Column(Boolean)


class Token(Base):
    __tablename__ = "tokens"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer)
    token = Column(String)
    is_active = Column(Boolean)
    expires_at = Column(DateTime(timezone=True))


class AdminToken(Base):
    __tablename__ = "admin_tokens"
    id = Column(Integer, primary_key=True)
    admin_id = Column(Integer)
    token = Column(String)
    is_active = Column(Boolean)
    expires_at = Column(DateTime(timezone=True))


class Interview(Base):
    __tablename__ = "interviews"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer)


class Resume(Base):
    __tablename__ = "resumes"
    id = Column(Integer, primary_key=True)


class InterviewMessage(Base):
    __tablename__ = "interview_messages"
    id = Column(Integer, primary_key=True)
    interview_id = Column(Integer)
    role = Column(String)
    score = Column(Float)
    content = Column(String)
    feedback = Column(String)
    question_index = Column(Integer)


class APIException(Exception):
    def __init__(self, status_code=400, message="", **kwargs):
        super().__init__(message)
        self.status_code = status_code


def source_class(relative, name, namespace):
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / relative), "exec"), namespace)
    return namespace[name]


def namespace():
    from typing import Dict, Optional, List
    from decimal import Decimal
    import math
    return dict(Dict=Dict, Optional=Optional, List=List, Decimal=Decimal, math=math,
                datetime=datetime, timedelta=timedelta, UTC=UTC, hashlib=hashlib,
                json=json, logging=logging, logger=logging.getLogger("a2.test"),
                AsyncSession=object, select=select, update=update, User=User,
                Admin=Admin, Token=Token, AdminToken=AdminToken, Interview=Interview,
                InterviewMessage=InterviewMessage, Resume=Resume,
                APIException=APIException, ValidationError=APIException,
                NotFoundError=APIException)


class FakeDB:
    """Evaluate real simple SQLAlchemy WHERE expressions; no database connection."""
    def __init__(self, *rows):
        self.rows = list(rows)
        self.pending = []
        self.commits = 0
        self.rollbacks = 0
        self.queries = []
        self.snapshot = [(row, dict(row.__dict__)) for row in rows]

    @staticmethod
    def matches(row, clauses):
        for clause in clauses:
            for part in getattr(clause, "clauses", [clause]):
                value = getattr(part.right, "value", None)
                if str(part.right) == "true":
                    value = True
                elif str(part.right) == "false":
                    value = False
                left = getattr(row, part.left.name)
                operator = part.operator.__name__
                matches = (left is not value) if operator == "is_not" else (
                    (left is value) if operator == "is_" else part.operator(left, value))
                if not matches:
                    return False
        return True

    async def execute(self, statement):
        self.queries.append(statement)
        if isinstance(statement, Update):
            rows = [r for r in self.rows if r.__tablename__ == statement.table.name]
            for row in rows:
                if self.matches(row, statement._where_criteria):
                    for key, value in statement._values.items():
                        setattr(row, getattr(key, "name", key), value.value)
            return None
        entity = statement.column_descriptions[0]["entity"]
        rows = [r for r in self.rows + self.pending
                if type(r) is entity and self.matches(r, statement._where_criteria)]
        return SimpleNamespace(scalar_one_or_none=lambda: rows[0] if rows else None,
                               scalars=lambda: SimpleNamespace(all=lambda: rows))

    def add(self, row):
        if row.id is None:
            row.id = len(self.rows) + len(self.pending) + 1
        self.pending.append(row)

    async def flush(self):
        pass  # In-memory pending rows are already visible to this session.

    async def commit(self):
        self.commits += 1
        self.rows.extend(self.pending)
        self.pending.clear()
        self.snapshot = [(r, dict(r.__dict__)) for r in self.rows]

    async def rollback(self):
        self.rollbacks += 1
        self.pending.clear()
        for row, values in self.snapshot:
            row.__dict__.clear()
            row.__dict__.update(values)


class IsolatedCase(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        old = Path.cwd()
        os.chdir(self.directory)
        self.stack.callback(os.chdir, old)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.runner = self.stack.enter_context(asyncio.Runner())
        self.attempts = []

        def deny(*args, **kwargs):
            self.attempts.append(True)
            raise AssertionError("External network forbidden")

        for target in ("socket.socket.connect", "socket.socket.connect_ex",
                       "socket.socket.sendto", "socket.create_connection", "socket.getaddrinfo"):
            self.stack.enter_context(patch(target, side_effect=deny))

    def tearDown(self):
        self.assertEqual(self.attempts, [])

    def run_async(self, coroutine):
        return self.runner.run(coroutine)
