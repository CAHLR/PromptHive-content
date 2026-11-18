# Import CORS at the top of your file
from __future__ import annotations

import hmac
import json
import os
from typing import List, Optional
from urllib.parse import unquote

import flask_security
from flask import Flask, Response, jsonify, request
from flask_cors import CORS
from flask_mongoengine import MongoEngine
from flask_security import (
    MongoEngineUserDatastore,
    RoleMixin,
    Security,
    UserMixin,
    current_user,
    login_required,
)
from flask_wtf import csrf
from mongoengine import DoesNotExist

import time

import abscribe_backend.services.chatgpt_service as chatgpt_service
import abscribe_backend.services.event_logger as event_logger
import abscribe_backend.services.recipe_service as recipe_service
#from abscribe_backend.database.mongo_connection import db
from abscribe_backend.models.authentication_models.user import Role, User
from abscribe_backend.models.chunk import Chunk
from abscribe_backend.models.document import Document
from abscribe_backend.models.version import Version
from abscribe_backend.services.chunk_service import (
    add_chunk,
    get_chunk,
    get_chunks,
    remove_chunk,
)
from abscribe_backend.services.document_service import (
    create_document,
    delete_document,
    get_document,
    get_documents,
    update_document,
)
from abscribe_backend.services.feedback_item_chunk_service import (
    add_chunk_feedback,
    get_chunk_feedback,
    get_chunk_feedbacks,
    remove_chunk_feedback,
)
from abscribe_backend.services.feedback_item_document_service import (
    add_doc_feedback,
    get_doc_feedback,
    get_doc_feedbacks,
    remove_doc_feedback,
)
from abscribe_backend.services.feedback_item_version_service import (
    add_version_feedback,
    get_version_feedback,
    get_version_feedbacks,
    remove_version_feedback,
)
from abscribe_backend.services.google_sheet_service import (
    get_file_name,
    get_sheet_data,
    get_sheet_titles,
    append_rows,
    create_new_sheet,
    delete_rows,
    update_columns
)
# Prompt library now lives in Firestore (shares the event_logger client).
# The MongoEngine implementation in prompt_library_service.py is kept for
# reference / rollback but is no longer wired to the routes.
from abscribe_backend.services.prompt_library_firestore import (
    LibraryUnavailable,
    archive_all_prompts,
    archive_prompt,
    archive_with_admin,
    commit_prompt_hint,
    commit_scratchpad_prompt_hint, get_active_prompt_hints,
    get_all_active_prompts,
    toggle_like_prompt_hint,
    trace_prompt,
    update_lessons_tested_on_prompt_hint,
    visualise_scratchpad_prompt_tree, visualize_prompt_tree,
)
from abscribe_backend.services.version_service import (
    add_version,
    remove_version,
    update_version,
)

app = Flask(__name__)

# Initialise Firestore event logger. Safe no-op when credentials are absent.
event_logger.init()

# Optional shared access code for public Pages -> Quadro API.
# When unset/empty, the check is off (local development unchanged).
PROMPTHIVE_ACCESS_CODE = (os.environ.get("PROMPTHIVE_ACCESS_CODE") or "").strip()
if not PROMPTHIVE_ACCESS_CODE:
    app.logger.warning(
        "PROMPTHIVE_ACCESS_CODE is not set; access code check is disabled"
    )


@app.before_request
def require_prompthive_access_code():
    if not PROMPTHIVE_ACCESS_CODE:
        return None
    if request.method == "OPTIONS":
        return None
    path = request.path or ""
    if not (path.startswith("/chatGPT/") or path.startswith("/api/")):
        return None
    if path.startswith("/api/course-materials/"):
        return None
    provided = request.headers.get("X-PromptHive-Access-Code") or ""
    if not hmac.compare_digest(provided, PROMPTHIVE_ACCESS_CODE):
        return jsonify({"error": "access code required"}), 401
    return None


from abscribe_backend.course_materials_api import course_materials
app.register_blueprint(course_materials)


# Add this line after initializing your Flask app
CORS(
    app,
    origins=["http://localhost:5173", "http://127.0.0.1:5173", "https://cahlr.github.io", "http://127.0.0.1:8080", "https://quadro.ist.berkeley.edu:8080", "https://cahlrprompthive.netlify.app", "http://localhost:5174", "http://127.0.0.1:5174"],
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-PromptHive-Access-Code", "ngrok-skip-browser-warning", "X-Course-Materials-Request"],
    supports_credentials=True
)

#db.init_app(app)


# Helper function to convert objects to JSON serializable dictionaries
def to_dict(obj):
    return obj.to_mongo().to_dict() if obj else None


# Set up some variables used for security.

# Generate a nice key using secrets.token_urlsafe()
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "Not actually very secret.")
if len(os.environ.get("COURSE_MATERIALS_SECRET_KEY", "")) >= 32:
    app.config.update(SECRET_KEY=os.environ["COURSE_MATERIALS_SECRET_KEY"],
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_DOMAIN=False, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=os.environ.get("COURSE_MATERIALS_LOCAL_DEV") != "1")
# Bcrypt is set as default SECURITY_PASSWORD_HASH, which requires a salt
# Generate a good salt using: secrets.SystemRandom().getrandbits(128)
app.config["SECURITY_PASSWORD_SALT"] = os.environ.get(
    "SECURITY_PASSWORD_SALT", "Joseph Jay Williams"
)
# Don't worry if email has findable domain
app.config["SECURITY_EMAIL_VALIDATOR_ARGS"] = {"check_deliverability": False}

#user_datastore = MongoEngineUserDatastore(db, User, Role)
#app.security = Security(app, user_datastore)

ADMIN_ROLE_NAME = "admin"
USER_ROLE_NAME = "user"
# # TODO: This starter data initialisation is made only for testing locally. Please don't actually push!
# adminRole = Role(name=ADMIN_ROLE_NAME, description="Administrative privileges. Can do anything.")
# userRole = Role(name=USER_ROLE_NAME, description="A general user, and all normal permissions. Usually self-crud and viewing unowned docs.")
# adminRole.save()
# userRole.save()

app.config["WTF_CSRF_ENABLED"] = False


# Document endpoints
@app.route("/documents", methods=["GET"])
def get_documents_route() -> Response:
    encoded_query: Optional[str] = request.args.get("query")
    encoded_filter: Optional[str] = request.args.get("filter")
    search_query: Optional[str] = unquote(encoded_query) if encoded_query else ""
    date_filter: Optional[str] = unquote(encoded_filter) if encoded_filter else ""
    documents: List[Document] = get_documents(search_query, date_filter)
    if current_user.is_authenticated and current_user.has_role(ADMIN_ROLE_NAME):
        # Return all docs, period.
        response_data: List[dict] = [
            document.to_dict() if type(document) is not str else document
            for document in documents
        ]
    elif current_user.is_authenticated and current_user.has_role(USER_ROLE_NAME):
        # Return all docs with this owner.
        response_data: List[dict] = [
            document.to_dict() if type(document) is not str else document
            for document in documents
            if type(document) is str or document.owner is current_user
        ]
    else:
        # Return all docs with no owner in the event that they don't have specific permissions, for backwards compatibility.
        response_data: List[dict] = [
            document.to_dict() if type(document) is not str else document
            for document in documents
            if type(document) is str or document.owner is None
        ]

    return Response(json.dumps(response_data), content_type="application/json")


@app.route("/documents", methods=["POST"])
def create_document_route() -> Response:
    content: Optional[str] = request.json.get("content")
    name: Optional[str] = request.json.get("name")
    last_modified_date: Optional[str] = request.json.get("last_modified_date")
    if content is None:
        return jsonify({"error": "Content is required"}), 400
    if name is None:
        document: Document = create_document(content, last_modified_date)
    else:
        document: Document = create_document(content, name, last_modified_date)

    # Set the owner if it's applicable.
    if current_user.is_authenticated:
        document.owner = current_user
        document.save()
    else:
        document.owner = None

    return Response(document.to_json(), status=201, content_type="application/json")


@app.route("/documents/<document_id>", methods=["GET"])
def get_document_route(document_id: str) -> Response:
    document: Optional[Document] = get_document(document_id)
    if document:
        # Logged in users can grab their docs. Admins can grab anything.
        if current_user.is_authenticated and (
            document.owner == current_user
            and current_user.hasRole(USER_ROLE_NAME)
            or current_user.hasRole(ADMIN_ROLE_NAME)
        ):
            return Response(document.to_json(), content_type="application/json")

        # People can always view unowned documents, for demo purposes.
        elif not document.owner:
            return Response(document.to_json(), content_type="application/json")
    else:
        return (
            jsonify(
                {"error": "Document that this user is allowed to access is not found"}
            ),
            404,
        )


@app.route("/documents/<document_id>", methods=["PUT"])
def update_document_route(document_id: str) -> Response:
    updated_content: Optional[str] = request.json.get("content")
    updated_name: Optional[str] = request.json.get("name")

    if updated_content is None and updated_name is None:
        return jsonify({"error": "Content and Name is required"}), 400

    document = get_document(document_id)
    # Admins can update any doc.
    if current_user.is_authenticated and current_user.has_role(ADMIN_ROLE_NAME):
        updated_document: Optional[Document] = update_document(
            document_id, updated_content, updated_name
        )
    # Regular users can update their documents.
    elif (
        current_user.is_authenticated
        and current_user.has_role(USER_ROLE_NAME)
        and document.owner == current_user
    ):
        updated_document: Optional[Document] = update_document(
            document_id, updated_content, updated_name
        )
    # Anybody can play with unowned documents.
    elif document.owner is None:
        updated_document: Optional[Document] = update_document(
            document_id, updated_content, updated_name
        )

    if updated_document:
        response_data: dict = updated_document.to_dict()
        return Response(json.dumps(response_data), content_type="application/json")
    else:
        return Response(
            "We couldn't find a document you were allowed to edit.", status=404
        )


@app.route("/documents/<document_id>", methods=["DELETE"])
def delete_document_route(document_id: str) -> Response:
    document = get_document(document_id)
    # If the document is the user's, or the user is an admin, or the document is unowned.
    if (
        current_user.is_authenticated
        and (
            document.owner == current_user
            and current_user.has_role(USER_ROLE_NAME)
            or current_user.has_role(ADMIN_ROLE_NAME)
        )
        or document.owner is None
    ):
        success: bool = delete_document(document_id)

    if success:
        return jsonify({"result": "Document deleted"}), 200
    else:
        return jsonify({"error": "Document not found"}), 404


# Chunk endpoints
@app.route("/documents/<document_id>/chunks", methods=["GET"])
def get_chunks_route(document_id: str) -> Response:
    chunks: Optional[list[Chunk]] = get_chunks(document_id)
    if chunks is not None:
        response_data = [chunk.to_dict() for chunk in chunks]
        return Response(json.dumps(response_data), content_type="application/json"), 200
    else:
        return jsonify({"error": "Document not found"}), 404


@app.route("/documents/<document_id>/chunks", methods=["POST"])
def add_chunk_route(document_id: str) -> Response:
    chunk_data: Optional[str] = request.json.get("chunk_data")
    if chunk_data is None:
        return jsonify({"error": "Chunk data is required"}), 400

    chunk: Optional[Chunk] = add_chunk(document_id, chunk_data)
    if chunk:
        return Response(chunk.to_json(), status=201, content_type="application/json")
    else:
        return jsonify({"error": "Document not found"}), 404


@app.route("/documents/<document_id>/chunks/<int:chunk_index>", methods=["GET"])
def get_chunk_route(document_id: str, chunk_index: int) -> Response:
    chunk: Optional[Chunk] = get_chunk(document_id, chunk_index)
    if chunk:
        return Response(chunk.to_json(), content_type="application/json"), 200
    else:
        return jsonify({"error": "Chunk not found"}), 404


@app.route("/documents/<document_id>/chunks/<int:chunk_index>", methods=["DELETE"])
def remove_chunk_route(document_id: str, chunk_index: int) -> Response:
    chunk: Optional[Chunk] = remove_chunk(document_id, chunk_index)
    if chunk:
        return Response(chunk.to_json(), content_type="application/json"), 200
    else:
        return jsonify({"error": "Chunk not found"}), 404


# Version endpoints
@app.route(
    "/documents/<document_id>/chunks/<int:chunk_index>/versions", methods=["POST"]
)
def add_version_route(document_id: str, chunk_index: int) -> Response:
    version_data: Optional[str] = request.json.get("version_data")
    if version_data is None:
        return jsonify({"error": "Version data is required"}), 400

    version: Optional[Version] = add_version(document_id, chunk_index, version_data)
    if version:
        return Response(version.to_json(), status=201, content_type="application/json")
    else:
        return jsonify({"error": "Failed to add version"}), 404


@app.route(
    "/documents/<document_id>/chunks/<int:chunk_index>/versions", methods=["PUT"]
)
def update_version_route(document_id: str, chunk_index: int) -> Response:
    version_data: Optional[dict] = request.json.get("version_data")
    version_index: Optional[int] = request.json.get("version_index")

    if version_data is None or "text" not in version_data or version_index is None:
        return jsonify({"error": "Version data and index are required"}), 400

    updated_text: str = version_data["text"]

    version: Optional[Version] = update_version(
        document_id, chunk_index, version_index, updated_text
    )
    if version:
        return Response(version.to_json(), content_type="application/json"), 200
    else:
        return jsonify({"error": "Version not found"}), 404


@app.route(
    "/documents/<document_id>/chunks/<int:chunk_index>/versions/<int:version_index>",
    methods=["DELETE"],
)
def remove_version_route(
    document_id: str, chunk_index: int, version_index: int
) -> Response:
    version: Optional[Version] = remove_version(document_id, chunk_index, version_index)
    if version:
        return Response(version.to_json(), content_type="application/json"), 200
    else:
        return jsonify({"error": "Version not found"}), 404


@app.route("/chatGPT/models", methods=["GET"])
def list_chat_models() -> Response:
    """Live OpenAI models, filtered to vision-capable chat IDs for the UI picker."""
    try:
        models = chatgpt_service.list_vision_capable_models()
        return jsonify({"models": models, "default": chatgpt_service.DEFAULT_GPT_MODEL})
    except Exception as e:
        print(f"list_chat_models failed: {e}")
        return (
            jsonify(
                {
                    "error": str(e),
                    "models": [{"id": chatgpt_service.DEFAULT_GPT_MODEL, "created": None}],
                    "default": chatgpt_service.DEFAULT_GPT_MODEL,
                }
            ),
            200,
        )


@app.route("/chatGPT/json", methods=["POST"])
def get_json_message() -> Response:
    request_json = request.get_json()
    messages = request_json["messages"]
    response_type = request_json.get("type", "default")
    model = request_json.get("model", "gpt-4o")
    temperature = request_json.get("temperature", 0)
    isopen = request_json.get("isOpenEnded", False)
    difficulty_filter = request_json.get("difficultyFilter")
    log_context = {
        "userId": request_json.get("userId"),
        "userName": request_json.get("userName"),
        "spreadsheetId": request_json.get("spreadsheetId"),
        "lessonName": request_json.get("lessonName"),
        "promptId": request_json.get("promptId"),
        "generationRunId": request_json.get("generationRunId"),
        "clientTimestamp": request_json.get("clientTimestamp"),
        "useOriginalItemsAsTemplates": request_json.get("useOriginalItemsAsTemplates"),
        "useGeneratedItemsAsTemplates": request_json.get("useGeneratedItemsAsTemplates"),
        "batchNumber": request_json.get("batchNumber"),
        "userPromptText": request_json.get("userPromptText"),
        "userPromptHash": request_json.get("userPromptHash"),
        "scratchpadId": request_json.get("scratchpadId"),
    }

    start = time.monotonic()
    error: str | None = None
    result: dict | None = None
    try:
        result = chatgpt_service.get_structured_output(
            messages=messages,
            response_type=response_type,
            model=model,
            temperature=temperature,
            isopen=isopen,
            difficulty_filter=difficulty_filter,
        )
        return result
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        if response_type == "oatutor_questions":
            try:
                event_logger.log_question_generation(
                    messages=messages,
                    requested_model=(result or {}).get("modelRequested") or model,
                    used_model=(result or {}).get("model") or model,
                    model_fallback=bool((result or {}).get("modelFallback")),
                    model_fallback_reason=(result or {}).get("modelFallbackReason"),
                    temperature=temperature,
                    difficulty_filter=difficulty_filter,
                    questions=(result or {}).get("questions"),
                    context=log_context,
                    duration_ms=(time.monotonic() - start) * 1000.0,
                    error=error,
                )
            except Exception as log_exc:  # pragma: no cover - defensive
                print(f"event_logger.log_question_generation failed: {log_exc}")


@app.route("/chatGPT/calibrate-generated-questions", methods=["POST"])
def calibrate_generated_questions_route() -> Response:
    """Feature 1: one Rasch/filter pass on already-generated OATutor rows.

    Used by Option B (T template generations, then a single calibration).

    Accept negotiation:
    - ``Accept: text/event-stream`` → SSE progress events, final ``result`` payload
    - ``Accept: application/json`` (or default) → today's one-shot JSON body
    """
    request_json = request.get_json(silent=True) or {}
    questions = request_json.get("questions", [])
    difficulty_filter = request_json.get("difficultyFilter")
    log_context = {
        "userId": request_json.get("userId"),
        "userName": request_json.get("userName"),
        "spreadsheetId": request_json.get("spreadsheetId"),
        "lessonName": request_json.get("lessonName"),
        "promptId": request_json.get("promptId"),
        "generationRunId": request_json.get("generationRunId"),
        "clientTimestamp": request_json.get("clientTimestamp"),
        "useOriginalItemsAsTemplates": request_json.get("useOriginalItemsAsTemplates"),
        "useGeneratedItemsAsTemplates": request_json.get("useGeneratedItemsAsTemplates"),
        "batchNumber": request_json.get("batchNumber"),
        "userPromptHash": request_json.get("userPromptHash"),
        "scratchpadId": request_json.get("scratchpadId"),
    }

    def build_payload(progress_callback=None):
        from abscribe_backend.services.question_difficulty.difficulty_service import (
            sanitize_for_json,
        )

        start = time.monotonic()
        error: str | None = None
        payload: dict = {}
        try:
            filtered, meta = chatgpt_service.apply_difficulty_filter_to_questions(
                questions,
                difficulty_filter,
                progress_callback=progress_callback,
            )
            payload = sanitize_for_json(
                {"questions": filtered, "difficultyFilter": meta}
            )
            return payload
        except Exception as e:
            # Soft-fail (same shape as historical HTTP 200 error body).
            print(f"calibrate-generated-questions failed: {e}")
            error = str(e)
            payload = sanitize_for_json(
                {
                    "questions": questions,
                    "difficultyFilter": {
                        "calibrationStatus": "error",
                        "error": str(e),
                    },
                    "difficultyFilterError": str(e),
                }
            )
            return payload
        finally:
            try:
                difficulty_meta = (payload or {}).get("difficultyFilter") or {}
                event_logger.log_difficulty_estimation(
                    source="after_generation",
                    questions=questions,
                    scores=difficulty_meta.get("evaluations"),
                    n_respondents=(difficulty_filter or {}).get("nRespondents"),
                    calibration_meta=difficulty_meta,
                    context=log_context,
                    duration_ms=(time.monotonic() - start) * 1000.0,
                    error=error,
                )
            except Exception as log_exc:  # pragma: no cover - defensive
                print(
                    f"event_logger.log_difficulty_estimation (after_generation) "
                    f"failed: {log_exc}"
                )

    from abscribe_backend.services.question_difficulty.sse_progress import (
        client_wants_sse,
        sse_response_from_worker,
    )

    if client_wants_sse():
        return sse_response_from_worker(build_payload)

    return jsonify(build_payload(None)), 200


@app.route("/chatGPT/score-existing-questions", methods=["POST"])
def score_existing_questions_route() -> Response:
    """Feature 2: score EXISTING sheet questions on the same difficulty scale as
    generated ones (reuses the generation calibration engine; no filtering).

    Accept negotiation mirrors calibrate-generated-questions (SSE vs JSON).
    """
    request_json = request.get_json(silent=True) or {}
    questions = request_json.get("questions", [])
    n_respondents = request_json.get("nRespondents")
    log_context = {
        "userId": request_json.get("userId"),
        "userName": request_json.get("userName"),
        "spreadsheetId": request_json.get("spreadsheetId"),
        "lessonName": request_json.get("lessonName"),
        "promptId": request_json.get("promptId"),
        "generationRunId": request_json.get("generationRunId"),
        "clientTimestamp": request_json.get("clientTimestamp"),
        "useOriginalItemsAsTemplates": request_json.get("useOriginalItemsAsTemplates"),
        "useGeneratedItemsAsTemplates": request_json.get("useGeneratedItemsAsTemplates"),
        "batchNumber": request_json.get("batchNumber"),
        "userPromptHash": request_json.get("userPromptHash"),
        "scratchpadId": request_json.get("scratchpadId"),
    }

    def build_payload(progress_callback=None):
        from abscribe_backend.services.question_difficulty.difficulty_service import (
            sanitize_for_json,
            score_existing_questions,
        )

        start = time.monotonic()
        error: str | None = None
        payload: dict = {}
        try:
            payload = sanitize_for_json(
                score_existing_questions(
                    questions,
                    n_respondents=n_respondents,
                    progress_callback=progress_callback,
                )
            )
            return payload
        except Exception as e:
            print(f"score-existing-questions failed: {e}")
            error = str(e)
            payload = {
                "scores": [],
                "calibrationStatus": "error",
                "error": str(e),
            }
            return payload
        finally:
            try:
                event_logger.log_difficulty_estimation(
                    source="estimate_button",
                    questions=questions,
                    scores=(payload or {}).get("scores"),
                    n_respondents=n_respondents,
                    calibration_meta={
                        "calibrationStatus": (payload or {}).get("calibrationStatus"),
                        "respondentTemperature": (payload or {}).get(
                            "respondentTemperature"
                        ),
                        "respondentAbility": (payload or {}).get("respondentAbility"),
                        # Response-matrix cells (respondents x items) and NA
                        # count come from score_existing_questions so the
                        # Firestore record stamps the real cell workload
                        # instead of OATutor row counts.
                        "cellsTotal": (payload or {}).get("cellsTotal"),
                        "cellsMissingCount": (payload or {}).get(
                            "cellsMissingCount"
                        ),
                        "meta": (payload or {}).get("meta"),
                    },
                    context=log_context,
                    duration_ms=(time.monotonic() - start) * 1000.0,
                    error=error,
                )
            except Exception as log_exc:  # pragma: no cover - defensive
                print(
                    f"event_logger.log_difficulty_estimation (estimate_button) "
                    f"failed: {log_exc}"
                )

    from abscribe_backend.services.question_difficulty.sse_progress import (
        client_wants_sse,
        sse_response_from_worker,
    )

    if client_wants_sse():
        return sse_response_from_worker(build_payload)

    return jsonify(build_payload(None)), 200


@app.route("/chatGPT/chat", methods=["POST"])
def get_chat_message() -> Response:
    """Endpoint for receiving a chat message from the chatGPT API. Sending more messages may be difficult."""
    message_json = request.get_json()
    messages = message_json["messages"]
    model = message_json.get("model", "gpt-4o")  # Default to 'gpt-4o' if not provided
    type = message_json.get("type", "text")  # Default to 'text' if not provided
    temperature = message_json.get("temperature", 0)
    chat_stream = chatgpt_service.get_chat(
        messages, model, type, temperature
    )  # returns an event stream we can iterate over.
    tab = "table" in message_json["messages"][-1]["content"]
    lst = "list" in message_json["messages"][-1]["content"]

    def stream_chat():
        for chunk in chat_stream:
            try:
                content: str = (
                    chunk.choices[0].delta.content or ""
                )  # chunk.choices[0].delta.content may be None
            except KeyError:
                content = ""
            # 'data:' and newlines format each text block as a new server sent event. See the documentation on MDN.
            yield f"data: {content}\n\n"
            # Start a new paragraph if we see a newline.
            if "\n" in content and type == "text":
                if not tab and not lst:
                    yield f"data: <br/><br/>\n\n"
                else:
                    yield f"data: \n\n"

    return app.response_class(stream_chat(), mimetype="text/event-stream")


@app.route(
    "/recipes/", defaults={"recipe_id": None}, methods=["GET"]
)  # I think this is necessary because there are several routes pointing here so there will be a redirect to the backslash.
@app.route("/recipes/<string:recipe_id>", methods=["GET"])
def get_recipes(recipe_id: str | None = None) -> Response:
    """Endpoint for retrieving recipes from the database. If an id is passed in, a list of recipes matching that name
    will be retrieved. If an id is not passed in, every recipe will be retrieved."""
    if recipe_id is not None:
        recipe = recipe_service.get_recipe(recipe_id)
        if recipe:
            return recipe.to_dict()
        else:
            return Response(
                "An error occurred when retrieving this recipe for some reason.", 500
            )
    else:
        recipes = recipe_service.get_recipes()
        if recipes:
            return recipes
        else:
            app.logger.info(
                "We found no recipes when trying to get them all. There may be an error but there could also"
                + "just be no recipes."
            )
            return []


@app.route("/recipes/create", methods=["POST"])
def create_recipe() -> Response:
    """Endpoint for adding recipes to the database. Send the data as part of the request body since it could be big."""
    data = request.get_json()
    recipe = recipe_service.create_recipe(
        data["id"], data["name"], data["prompt"], str(data["home_document_id"])
    )
    if recipe:
        return Response(
            response=recipe.to_dict(), status=200, mimetype="application/json"
        )
    else:
        return Response("Something went wrong with recipe creation", 500)


@app.route("/recipes/update", methods=["POST"])
def update_recipe() -> Response:
    """Endpoint for updating recipes in the database. Send the data as part of the request body since it could be big."""
    data = request.get_json()
    recipe = recipe_service.update_recipe(
        data["recipe_id"], data["new_name"], data["new_prompt"]
    )
    if recipe:
        return Response(
            response=recipe.to_dict(), status=200, mimetype="application/json"
        )
    else:
        return Response(
            response="Something weird happened when we were trying to update the recipe.",
            status=500,
        )


@app.route("/recipes/delete/<string:recipe_id>", methods=["DELETE"])
def delete_recipe(recipe_id: str) -> Response:
    """Endpoint for deleting a recipe from the database by a particular name."""
    success = recipe_service.delete_recipe(recipe_id)
    if success:
        return Response(status=200)
    else:
        return Response(
            response="Something went wrong when we tried deleting that recipe.",
            status=500,
        )


@app.route("/user/<string:email>", methods=["DELETE"])
@app.route("/user", methods=["POST"])
def create_user(email=None):
    """Endpoint for CRUD with a user in the database. Send the user's details by HTTP**S** request, please!
    Since we're sending data as plaintext to start with, it's seriously important to use HTTPS or people can spy on our
    users without even paying us.

    Currently, requires that you pass the user's email, unhashed password, and roles to the endpoint.
    """

    data = request.get_json()
    if request.method == "POST":
        # Flask-Security doesn't actually do any normalisation on the email and password, we gotta check it ourselves...
        try:
            validated_email = flask_security.MailUtil(app).validate(email=data["email"])
            password_warnings, password_normalised = flask_security.PasswordUtil(
                app
            ).validate(data["password"], is_register=True)
            if password_warnings is not None:
                raise ValueError

        except ValueError:
            return Response(
                response="Email normalisation and validation failed, maybe an insecure email entered.",
                status=406,
            )

        # Invariant: The email and the password are now safe to use in the database.
        secure_password = flask_security.utils.hash_password(password_normalised)
        user = user_datastore.create_user(
            email=validated_email, password=secure_password
        )

        # Consider all generally created users to be just that: users.
        user_datastore.add_role_to_user(user, "user")

        return user.to_dict()

    elif request.method == "DELETE":
        user_to_deactivate = user_datastore.find_user(
            email=email
        )  # Emails are unique so this is fine.
        user_datastore.deactivate_user(user_to_deactivate)

    # TODO: consider allowing users to change their credentials. Make them log in first, though.


@app.route("/login", methods=["POST"])
def login():
    """An endpoint for logging in a user, whose email and password are provided as part of a POST request."""
    data = request.json
    # Take the user's email and password.
    plaintext_password = data["password"]
    email = data["email"]

    # Validate and normalise the input.
    validated_email = flask_security.MailUtil(app).validate(email)
    password_warnings, validated_password = flask_security.PasswordUtil(app).validate(
        plaintext_password, False
    )

    # Check if we can find a user, and if the password is correct.
    user = user_datastore.find_user(email=validated_email, case_sensitive=False)
    if user is None:
        return Response(response="The provided user could not be found.", status=240)

    password_legit = flask_security.verify_password(validated_password, user.password)

    if password_legit is False:
        return Response(
            response="The password didn't match what we had in our database.",
            status=403,
        )

    # Invariant: at this point we found a user and the password for it matches.
    flask_security.login_user(user=user, authn_via=["password"])
    return Response(response="You should be logged in now.", status=200)


@login_required
@app.route("/logout", methods=["POST"])
def logout():
    """An endpoint for logging out the current user."""
    flask_security.logout_user()


# Document Feedback endpoints
@app.route("/documents/<document_id>/feedback", methods=["POST"])
def add_document_feedback_route(document_id: str) -> Response:
    """Endpoint for adding feedback to a document."""
    feedback_data = request.json
    feedback = add_doc_feedback(document_id, feedback_data)
    if feedback:
        return Response(feedback.to_json(), status=201, content_type="application/json")
    else:
        return jsonify({"error": "Failed to add feedback"}), 404


@app.route("/documents/<document_id>/feedback", methods=["GET"])
def get_document_feedbacks_route(document_id: str) -> Response:
    """Endpoint for getting all feedbacks for a document."""
    feedbacks = get_doc_feedbacks(document_id)
    if feedbacks is not None:
        response_data = [feedback.to_dict() for feedback in feedbacks]
        return Response(json.dumps(response_data), content_type="application/json"), 200
    else:
        return jsonify({"error": "Document not found"}), 404


@app.route("/documents/<document_id>/feedback/<feedback_id>", methods=["GET"])
def get_document_feedback_route(document_id: str, feedback_id: str) -> Response:
    """Endpoint for getting a feedback for a document."""
    feedback = get_doc_feedback(document_id, feedback_id)
    if feedback:
        return Response(feedback.to_json(), content_type="application/json"), 200
    else:
        return jsonify({"error": "Feedback not found"}), 404


@app.route("/documents/<document_id>/feedback/<feedback_id>", methods=["DELETE"])
def remove_document_feedback_route(document_id: str, feedback_id: str) -> Response:
    """Endpoint for removing a feedback for a document."""
    feedback = remove_doc_feedback(document_id, feedback_id)
    if feedback:
        return jsonify({"result": "Feedback removed"}), 200
    else:
        return jsonify({"error": "Feedback not found"}), 404


# Chunk Feedback endpoints
@app.route("/documents/<document_id>/chunks/<chunk_id>/feedback", methods=["POST"])
def add_chunk_feedback_route(document_id: str, chunk_id: str) -> Response:
    """Endpoint for adding feedback to a chunk."""
    feedback_data = request.json
    feedback = add_chunk_feedback(document_id, chunk_id, feedback_data)
    if feedback:
        return Response(feedback.to_json(), status=201, content_type="application/json")
    else:
        return jsonify({"error": "Failed to add feedback"}), 404


@app.route("/documents/<document_id>/chunks/<chunk_id>/feedback", methods=["GET"])
def get_chunk_feedbacks_route(document_id: str, chunk_id: str) -> Response:
    """Endpoint for getting all feedbacks for a chunk."""
    feedbacks = get_chunk_feedbacks(document_id, chunk_id)
    if feedbacks is not None:
        response_data = [feedback.to_dict() for feedback in feedbacks]
        return Response(json.dumps(response_data), content_type="application/json"), 200
    else:
        return jsonify({"error": "Chunk not found"}), 404


# Version feedback endpoints
@app.route("/documents/<document_id>/versions/<version_id>/feedback", methods=["POST"])
def add_version_feedback_route(document_id: str, version_id: str) -> Response:
    """Endpoint for adding feedback to a version."""
    feedback_data = request.json
    feedback = add_version_feedback(document_id, version_id, feedback_data)
    if feedback:
        return Response(feedback.to_json(), status=201, content_type="application/json")
    else:
        return jsonify({"error": "Failed to add feedback"}), 404


@app.route("/documents/<document_id>/versions/<version_id>/feedback", methods=["GET"])
def get_version_feedbacks_route(document_id: str, version_id: str) -> Response:
    """Endpoint for getting all feedbacks for a version."""
    feedbacks = get_version_feedbacks(document_id, version_id)
    if feedbacks is not None:
        response_data = [feedback.to_dict() for feedback in feedbacks]
        return Response(json.dumps(response_data), content_type="application/json"), 200
    else:
        return jsonify({"error": "Version not found"}), 404


# Google Sheets integration endpoints. Added for OATutor
@app.route("/api/google-sheets/data", methods=["GET"])
def get_google_sheets_data_api():
    spreadsheet_id = request.args.get("spreadsheetId")
    range_ = request.args.get("range")
    filter_criteria = request.args.get(
        "filter_criteria"
    )  # Get filter criteria from request
    if not spreadsheet_id or not range_:
        return jsonify({"error": "Spreadsheet ID and range are required."}), 400
    if filter_criteria:
        filter_criteria = json.loads(filter_criteria)  # Convert string to dictionary
    data = get_sheet_data(
        spreadsheet_id, range_, filter_criteria=filter_criteria
    )  # Pass filter_criteria
    return jsonify(data), 200


@app.route("/api/google-sheets/titles", methods=["GET"])
def get_google_sheets_titles_api():
    spreadsheet_id = request.args.get("spreadsheetId")
    if not spreadsheet_id:
        return jsonify({"error": "Spreadsheet ID is required."}), 400
    titles = get_sheet_titles(spreadsheet_id)
    return jsonify(titles), 200


@app.route("/api/google-sheets/file-name", methods=["GET"])
def get_google_sheets_file_name_api():
    spreadsheet_id = request.args.get("spreadsheetId")
    if not spreadsheet_id:
        return jsonify({"error": "Spreadsheet ID is required."}), 400
    file_name = get_file_name(spreadsheet_id)
    return jsonify(file_name), 200

@app.route("/api/google-sheets/append", methods=["POST"])
def append_to_google_sheet_api():
    try:
        data = request.json
        spreadsheet_id = data.get("spreadsheetId")
        range_ = data.get("range")
        values = data.get("values")
        if not spreadsheet_id or not range_ or not values:
            return jsonify({"error": "Spreadsheet ID, range, and values are required."}), 400
        result = append_rows(spreadsheet_id, range_, values)
        return jsonify(result), 200
    except Exception as e:
        print(f"Error occurred: {str(e)}")  # Log the error to console
        return jsonify({"error": "An internal error occurred."}), 500


@app.route("/api/google-sheets/create", methods=["POST"])
def create_google_sheet_api():
    data = request.json
    spreadsheet_id = data.get("spreadsheetId")
    sheet_title = data.get("sheetTitle")
    if not spreadsheet_id or not sheet_title:
        return jsonify({"error": "Spreadsheet ID and sheet title are required."}), 400
    result = create_new_sheet(spreadsheet_id, sheet_title)
    return jsonify(result), 200

@app.route("/api/google-sheets/delete-rows", methods=["POST"])
def delete_google_sheet_rows_api():
    try:
        data = request.json
        spreadsheet_id = data.get("spreadsheetId")
        sheet_name = data.get("sheetName")  # This should be the name of the sheet
        start_index = data.get("startIndex")
        end_index = data.get("endIndex")
        if not spreadsheet_id or not sheet_name or start_index is None or end_index is None:
            return jsonify({"error": "Spreadsheet ID, sheet name, start index, and end index are required."}), 400
        result = delete_rows(spreadsheet_id, sheet_name, start_index, end_index)
        print(f"Delete result: {result}")  # Log the result
        return jsonify(result), 200
    except Exception as e:
        print(f"Error occurred: {str(e)}")  # Log any other exceptions
        return jsonify({"error": "An internal error occurred."}), 500

@app.route("/api/google-sheets/update-columns", methods=["POST"])
def update_google_sheet_columns_api():
    data = request.json
    spreadsheet_id = data.get("spreadsheetId")
    range_ = data.get("range")
    values = data.get("values")
    if not spreadsheet_id or not range_ or not values:
        return jsonify({"error": "Spreadsheet ID, range, and values are required."}), 400
    result = update_columns(spreadsheet_id, range_, values)
    return jsonify(result), 200


@app.route("/api/prompt-library/active", methods=["GET"])
def get_active_prompts():
    spreadsheet_id = request.args.get("spreadsheetId")
    print(spreadsheet_id)
    if not spreadsheet_id:
        return jsonify({"error": "Spreadsheet ID is required."}), 400
    lesson_name = request.args.get("lessonName")
    # The shared prompt library lives in Firestore, which is optional for the
    # difficulty-filter / sheet-loading flow. If it isn't configured
    # (LibraryUnavailable) or errors, this must degrade to an empty list
    # rather than 500 and stall the frontend.
    try:
        return jsonify(get_active_prompt_hints(spreadsheet_id, lesson_name)), 200
    except Exception as e:
        print(f"prompt-library/active unavailable (returning empty): {e}")
        return jsonify([]), 200


@app.route("/api/prompt-library/commit-prompt", methods=["POST"])
def commit_prompt_hint_to_library():
    try:
        return jsonify(commit_prompt_hint(request.json)), 201
    except ValueError as e:
        return jsonify({"message": str(e)}), 400
    except Exception as e:
        # Saving to the shared library needs Firestore, which is optional
        # (LibraryUnavailable when unconfigured). Don't let its absence 500
        # and break the UI.
        print(f"commit-prompt skipped (library unavailable): {e}")
        return jsonify({"message": "prompt library unavailable; not saved"}), 200


@app.route("/api/prompt-library/toggle-like-prompt", methods=["POST"])
def toggle_like_prompt():
    user_id, prompt_id = request.json.get("userId"), request.json.get("promptId")
    try:
        toggle_like_prompt_hint(prompt_id, user_id)
        return jsonify({"message": "successful"}), 201
    except Exception as e:
        return jsonify({"message": str(e)}), 400


@app.route("/api/prompt-library/update-lessons-tested", methods=["PUT"])
def update_lessons_tested():
    new_lessons, prompt_id = request.json.get("newLessons"), request.json.get(
        "promptId"
    )
    try:
        update_lessons_tested_on_prompt_hint(prompt_id, new_lessons)
        return jsonify({"message": "updated lessons"}), 201
    except Exception as e:
        return jsonify({"message": str(e)}), 400


@app.route("/api/prompt-library/archive-prompt", methods=["PUT"])
def archive_prompt_in_library():
    prompt_id, user_id = request.json.get("promptId"), request.json.get("userId")
    if not prompt_id:
        return jsonify({"message": "promptId not provided"}), 400
    if not user_id:
        return jsonify({"message": "userId not provided"}), 400
    try:
        return jsonify(archive_prompt(prompt_id, user_id)), 201
    except Exception as e:
        return jsonify({"message": str(e)}), 400


@app.route("/api/prompt-library/visualize-tree", methods=["GET"])
def visualize_tree():
    spreadsheet_id = request.args.get("spreadsheetId")
    try:
        return jsonify(visualize_prompt_tree(spreadsheet_id)), 200
    except Exception as e:
        return jsonify({"message": str(e)}), 400


@app.route("/api/prompt-library/trace-prompt-origin", methods=["GET"])
def trace_promot_origin():
    prompt_id = request.args.get("promptId")
    if prompt_id is None:
        return jsonify({"message": "no promptId provided"}), 400
    try:
        response_data = trace_prompt(prompt_id)
        return jsonify(response_data), 200
    except DoesNotExist as e:
        return jsonify({"message": str(e)}), 400
    except (LibraryUnavailable, Exception) as e:
        return jsonify({"message": str(e)}), 400


@app.route("/api/prompt-library/archive-all-admin-vfr567uhfr", methods=["POST"])
def archive_all_by_admin():
    spreadsheet_id = request.json.get("spreadsheetId")
    if not spreadsheet_id:
        return jsonify({"message": "no spreadsheetId provided"}), 400
    try:
        archive_all_prompts(spreadsheet_id)
    except (LibraryUnavailable, Exception) as e:
        return jsonify({"message": str(e)}), 400
    return jsonify({"message": "successfully archived all"}), 200


@app.route("/api/prompt-library/archive-admin-vfr567uhfr", methods=["POST"])
def archive_by_admin():
    prompt_id = request.json.get("promptId")
    if prompt_id is None:
        return jsonify({"message": "no promptId provided"}), 400
    try:
        archive_with_admin(prompt_id)
    except (LibraryUnavailable, Exception) as e:
        return jsonify({"message": str(e)}), 400
    return jsonify({"message": "successfully archived all"}), 200


@app.route("/api/prompt-library/fetch-all-active-vfr567uhfr", methods=["GET"])
def fetch_all_active():
    spreadsheet_id = request.args.get("spreadsheetId")
    if not spreadsheet_id:
        return jsonify({"message": "no spreadsheetId provided"}), 400
    try:
        return jsonify(get_all_active_prompts(spreadsheet_id)), 200
    except Exception as e:
        print(f"prompt-library/fetch-all-active unavailable (returning empty): {e}")
        return jsonify([]), 200

@app.route("/api/prompt-library/commit-scratchpad-prompt", methods=["POST"])
def commit_prompt_from_scratchpad():
    try:
        return jsonify(commit_scratchpad_prompt_hint(request.json)), 201
    except ValueError as e:
        return jsonify({"message": str(e)}), 400
    except Exception as e:
        # Saving to the shared library needs Firestore, which is optional
        # (LibraryUnavailable when unconfigured). Don't let its absence 500
        # and break the generate flow in the browser.
        print(f"commit-scratchpad-prompt skipped (library unavailable): {e}")
        return jsonify({"message": "prompt library unavailable; not saved"}), 200

@app.route("/api/prompt-library/visualize-scratchpad-tree", methods=["GET"])
def visualize_scratchpad_tree():
    spreadsheet_id = request.args.get("spreadsheetId")
    try:
        return jsonify(visualise_scratchpad_prompt_tree(spreadsheet_id)), 200
    except Exception as e:
        return jsonify({"message": str(e)}), 400

def main():
    app.run(host="0.0.0.0", port=8080, debug=True)


if __name__ == "__main__":
    main()
