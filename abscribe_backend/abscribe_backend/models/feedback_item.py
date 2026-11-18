from abscribe_backend.database.mongo_connection import db

from .constants import DUMMY_DATE


# Abstract class for Feedback items
class FeedbackItem(db.Document):
    meta = {"abstract": True}
    content = db.StringField()
    creation_date = db.DateTimeField(required=True, default=DUMMY_DATE)
    ref_id = db.StringField(required=False)

    def to_dict(self):
        pass


# TODO: ADD STATUS (PENDING, RESOLVED, etc.)
