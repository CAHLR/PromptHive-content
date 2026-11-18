from abscribe_backend.database.mongo_connection import db

from .constants import DUMMY_DATE
from .version import Version


class Chunk(db.EmbeddedDocument):
    frontend_id = db.StringField(required=True)
    versions = db.ListField(db.EmbeddedDocumentField(Version))
    creation_date = db.DateTimeField(required=True, default=DUMMY_DATE)

    # FIXME: Maybe I should standardize the creation_date field since it's reused everywhere.
    def to_dict(self):
        return {
            "frontend_id": self.frontend_id,
            "versions": [version.to_dict() for version in self.versions],
            "creation_date": self.creation_date.isoformat()
            if self.creation_date
            else "Unknown",
        }
