"""This file is responsible for CRUD of Document objects in the database, as they are defined in models.document.py."""
# services/document_service.py
from __future__ import annotations
from abscribe_backend.models.document import Document
from typing import Optional
from datetime import datetime, timezone
from time import time


def create_document(
        content: str, name: str = "Untitled document", last_modified_date: datetime = datetime.now(timezone.utc)
    ) -> Document:
    """Create a new document."""
    document = Document(
        content=content, name=name, creation_date=datetime.now(timezone.utc), last_modified_date=last_modified_date
    )
    document.save()
    return document


def get_documents(
        search_query: str,
        date_filter: str
    ) -> list[Document | str]:
    """
    Get a list of all documents. They will be sorted by last date modified by default
    """
    documents = Document.objects().all()
    document_list: list[Document] = [document for document in documents]

    filtered_document_list: list[Document] = filter(
            lambda document : search_query.lower() in document.to_dict()["name"].lower(),
            document_list
            )

    sorted_document_list = sorted(
        filtered_document_list,
        key=lambda document : document.to_dict()["last_modified_date"], reverse=True
        )

    formatted_docs = _addSeparators(sorted_document_list, date_filter)
    return formatted_docs


def _addSeparators(documents: list[Document], date_filter: str) -> list[Document | str]:
        """
        Precondition:
            The documents are sorted in ascending order by last date modified

        Returns the list of documents with separators for documents that were last modified
        today, last week, last month and earlier.

        This is a helper function that should not be called outside of this file.
        """
        current_time = time()
        one_day = 86400
        one_week = 604800
        one_month = 2592000

        # Classify documents based on last_modified_date
        buckets = {
            "Today": [],
            "Last Week": [],
            "Last Month": [],
            "Earlier": [],
        }
        for doc in documents:
            time_elapsed = current_time - doc.last_modified_date.timestamp()

            if time_elapsed < one_day:
                buckets["Today"].append(doc)
            elif time_elapsed < one_week:
                buckets["Last Week"].append(doc)
            elif time_elapsed < one_month:
                buckets["Last Month"].append(doc)
            else:
                buckets["Earlier"].append(doc)

            # Clear documents list and append groups in order
        formatted_docs: list[Document | str] = []
        for bucket_name, bucket_documents in buckets.items():
            if bucket_documents and date_filter.strip() in bucket_name:
                formatted_docs.append(f"{bucket_name}\n")
                formatted_docs.extend(bucket_documents)
        return formatted_docs


def get_document(
        document_id: str
    ) -> Optional[Document]:
    """Get a specific document by ID"""
    document = Document.objects(id=document_id).first()
    return document if document else None


def update_document(
        document_id: str, updated_content: str, updated_name: str
    ) -> Optional[Document]:
    """Update the content of a document by ID"""
    print(f"Updating document with ID: {document_id}")

    if not updated_content and not updated_name:
        return None

    if updated_name:
        document = Document.objects(id=document_id).modify(
            new=True, set__content=updated_content, set__name=updated_name, set__last_modified_date=datetime.now(timezone.utc)
        )
    else:
        document = Document.objects(id=document_id).modify(
            new=True, set__content=updated_content, set__last_modified_date=datetime.now(timezone.utc)
        )

    return document if document else None


def delete_document(
        document_id: str
    ) -> bool:
    """Delete a document by ID"""
    result = Document.objects(id=document_id).delete()
    return result > 0
