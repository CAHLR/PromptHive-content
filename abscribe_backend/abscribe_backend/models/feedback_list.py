from abscribe_backend.database.mongo_connection import db
from abscribe_backend.models.document import Document
from abscribe_backend.models.feedback_item import FeedbackItem

from .constants import DUMMY_DATE


class FeedbackList(db.Document):
    doc_id = db.ReferenceField(Document)
    feedback_item_list = db.ListField(
        db.GenericReferenceField()
    )  # this must be generic because we are using an abstract class
    creation_date = db.DateTimeField(required=True, default=DUMMY_DATE)

    def to_dict(self):
        return {
            "_id": str(self.id),
            "doc_id": str(self.doc_id),
            "feedback_item_list": [
                feedback_item.to_dict() for feedback_item in self.feedback_item_list
            ],
            "creation_date": self.creation_date.isoformat()
            if self.creation_date is not None
            else "Unknown?",
        }
