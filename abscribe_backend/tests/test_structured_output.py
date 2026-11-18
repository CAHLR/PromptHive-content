import json
import unittest

from pydantic import BaseModel

from abscribe_backend.services.chatgpt_service import get_structured_output


class UofTStudent(BaseModel):
    name: str
    gender: str
    utorid: str
    email: str
    post: list[str]


class TestStructuredOutput(unittest.TestCase):
    pass
