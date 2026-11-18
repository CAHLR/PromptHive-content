from abscribe_backend.database.mongo_connection import db
from abscribe_backend.models.chunk import Chunk
from abscribe_backend.models.feedback_item import FeedbackItem

from .constants import DUMMY_DATE


# Concrete class for Feedback items for Versions
class FeedbackItemChunk(FeedbackItem):
    ref_id = db.StringField(required=True)

    def to_dict(self):
        return {
            "_id": str(self.id),
            "ref_id": self.ref_id,
            "content": self.content,
            "creation_date": self.creation_date.isoformat()
            if self.creation_date is not None
            else "Unknown?",
        }
