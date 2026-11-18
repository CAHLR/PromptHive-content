import mongoengine

from abscribe_backend.database.mongo_connection import db
from abscribe_backend.models.authentication_models.user import User

from .chunk import Chunk
from .constants import DUMMY_DATE


class Document(db.Document):
    content = db.StringField(required=True)
    name = db.StringField(required=False)
    chunks = db.ListField(db.EmbeddedDocumentField(Chunk))
    creation_date = db.DateTimeField(required=True, default=DUMMY_DATE)
    last_modified_date = db.DateTimeField(required=True, default=DUMMY_DATE)
    owner = db.ReferenceField(User, required=False, reverse_delete_rule=mongoengine.CASCADE)

    def to_dict(self):
        return {
            "_id": str(self.id),
            "owner": str(self.owner),
            "name": str(self.name),
            "content": self.content,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "creation_date": self.creation_date.isoformat()
            if self.creation_date is not None
            else "Unknown?",
            "last_modified_date": self.last_modified_date.isoformat()
            if self.last_modified_date is not None
            else "Unknown?",
            "owner": str(self.owner)
        }
