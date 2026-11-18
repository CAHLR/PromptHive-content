import Modal from "react-bootstrap/Modal";
import Button from "react-bootstrap/Button";
import { deleteDocument } from "../../../services/documentService";

export default function DeleteConfirmationModal({
    show, modalToggler, documentId, documentName, documentsFetcher
}) {
    return (
        <Modal
            show={show}
            onHide={() => modalToggler(false)}
            size="lg"
            aria-labelledby="contained-modal-title-vcenter"
            centered
            >
            <Modal.Header>
                <Modal.Title id="contained-modal-title-vcenter">
                    Delete "{documentName}"?
                </Modal.Title>
            </Modal.Header>
            <Modal.Body>
                <p>
                    This will delete the document permanently. You can't undo this
                    action.
                </p>
            </Modal.Body>
            <Modal.Footer>
                <Button onClick={() => modalToggler(false)} variant="light">
                    Cancel
                </Button>
                <Button
                    onClick={() => {
                        deleteDocument(documentId).then(
                            () => {
                                console.log("Document deleted in backend");
                                documentsFetcher()
                            }).catch(
                                (error) =>
                                console.error("Failed to delete document in backend:", error)
                            ).finally(() => {
                                modalToggler();
                            });
                    }}
                    variant="danger"
                    >
                    Delete
                </Button>
            </Modal.Footer>
        </Modal>
    );
}