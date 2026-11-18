from abscribe_backend.database.mongo_connection import db


class OATutorUser(db.Document):
    """A MongoEngine Model class for recipes in our database. """
    user_id = db.StringField(required=True)
    name = db.StringField(required=True)
