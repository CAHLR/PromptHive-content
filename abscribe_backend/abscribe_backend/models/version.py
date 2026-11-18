from datetime import datetime

from flask_mongoengine import MongoEngine

from abscribe_backend.database.mongo_connection import db

from .constants import DUMMY_DATE


class Version(db.EmbeddedDocument):
    frontend_id = db.StringField(required=True)
    text = db.StringField(required=True)
    creation_date = db.DateTimeField(required=True, default=DUMMY_DATE)

    def to_dict(self):
        return {
            "frontend_id": self.frontend_id,
            "text": self.text,
            "creation_date": self.creation_date.isoformat()
            if self.creation_date
            else "Unknown",
        }
