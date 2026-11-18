# AbTestingTools Backend

## Overview

AbTestingTools is a backend application that provides a set of tools for managing documents, chunks, and versions. The application allows users to create and manipulate documents that contain rich text content. Each document is divided into chunks, and each chunk can have multiple versions of text. The backend is built using Python and MongoDB, and it leverages the MongoEngine ODM for database operations.

## Features

- Document CRUD operations: Create, read, update, and delete documents.
- Chunk CRUD operations: Add and remove chunks within a document.
- Version CRUD operations: Add, update, and remove versions within a chunk.

## Models

### Document

- `content`: The content of the document (StringField).
- `chunks`: A list of chunks within the document (ListField of EmbeddedDocumentField).

### Chunk

- `versions`: A list of versions within the chunk (ListField of EmbeddedDocumentField).

### Version

- `text`: The text content of the version (StringField).

## Services

### Document Service

- `create_document(content)`: Create a new document with the given content.
- `get_documents()`: Get a list of all documents.
- `get_document(document_id)`: Get a specific document by ID.
- `update_document(document_id, updated_content)`: Update the content of a document by ID.
- `delete_document(document_id)`: Delete a document by ID.

### Chunk Service

- `add_chunk(document_id, chunk_data)`: Add a new chunk to a document by ID.
- `remove_chunk(document_id, chunk_index)`: Remove a chunk from a document by index.

### Version Service

- `add_version(document_id, chunk_index, version_text)`: Add a new version to a chunk in a document by index.
- `update_version(document_id, chunk_index, version_index, updated_text)`: Update a version in a chunk of a document by index.
- `remove_version(document_id, chunk_index, version_index)`: Remove a version from a chunk of a document by index.

## Tests

The application includes a comprehensive test suite that covers CRUD operations for documents, chunks, and versions. The tests also include scenarios with rich text content.

## Getting Started

### Prerequisites

- Python 3.9 or higher
- MongoDB

### Installation

1. Clone the repository via ssh: `git clone git@github.com:CAHLR/PromptHive.git`
2. If you don't have it installed, install [poetry](https://python-poetry.org/docs/#installing-with-the-official-installer)
3. Change to the project directory: `cd abscribe_backend`
4. Install dependencies: `poetry install`
5. Activate the virtual environment: `poetry shell`

### Environment Variables

The backend reads configuration values from a `.env` file. 
`cd abscribe_backend/abscribe_backend/services` and create a .env file with the following variables. Replace your-openai-api-key with your Open AI API key:

```env
# OpenAI
OPENAI_API_KEY=your-openai-api-key
```

Resolving 'command not found' errors: make sure that when you install new dependencies, that the corresponding executables are accessible in your PATH. 
If not, go into your system environment variables and add the absolute path to the folder containing the .exes. 

### Running the Application

1. Start the MongoDB server: `mongod`
2. Run the application: poetry run flask run

### Running the Tests

1. Run the test suite: `poetry run pytest`

## Tech Stack

This project utilizes a modern tech stack that includes MongoDB, MongoEngine, Flask, Poetry, and Pytest. Below is an overview of each technology and its role in the project:

### MongoDB

MongoDB is a NoSQL document-oriented database that stores data in flexible, JSON-like documents. It provides high performance, high availability, and easy scalability, making it a popular choice for web applications.

### MongoEngine

MongoEngine is an Object-Document Mapping (ODM) library for Python that works with MongoDB. It provides a layer of abstraction that allows developers to interact with MongoDB documents as if they were objects in Python. With MongoEngine, developers can define document schemas, perform CRUD operations, and validate data with ease.

### Flask

Flask is a lightweight web framework for Python that is used to build the backend of the application. It provides essential tools for routing, handling HTTP requests, and rendering responses. Flask's simplicity and flexibility make it a popular choice for building web APIs and microservices.

### Poetry

Poetry is a dependency management and packaging tool for Python. It allows developers to manage project dependencies, create virtual environments, and build and publish packages. Poetry's intuitive CLI and deterministic dependency resolution make it a powerful tool for managing Python projects.

### Pytest

Pytest is a testing framework for Python that is used to write and execute tests for the application. It provides a simple and expressive syntax for writing tests, along with powerful features for test discovery, parameterization, and fixtures. Pytest helps ensure the quality and correctness of the codebase.

## Contributing

Contributions are welcome! Please read the [CONTRIBUTING.md](CONTRIBUTING.md) file for details on how to contribute to the project.

## License

This project is licensed under the MIT License. See the [LICENSE.md](LICENSE.md) file for details.

## Acknowledgments

- The AbTestingTools team for their hard work and dedication to the project.
