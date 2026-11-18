import Modal from "react-bootstrap/Modal";
import Button from "react-bootstrap/Button";
import { updateDocument } from "../../../services/documentService"

export default function RenameModal({
    show, modalToggler, documentId, documentName, documentContent, documentNameSetter
}) {

    const renameDocument = (newName) => {
        updateDocument(documentId, documentContent, newName)
          .then(() => {
            documentNameSetter(newName)
            console.log("Document updated in backend")
            })
          .catch((error) => {
            console.error(
              "Failed to update document in backend when trying name:",
              error
            )
          })
        }

    const handleModalConfirm = () => {
        // First we get the name from the input field
        const inputElement = document.querySelector('#rename-modal-input');
        
        // Now we extract the new name and feed it into our helper
        const newName = inputElement.value
        renameDocument(newName)
        
        // Finally, we toggle the modal
        modalToggler()
    }

    return (
        <Modal
            show={show}
            onHide={() => modalToggler(false)}
            size="sm"
            aria-labelledby="contained-modal-title-vcenter"
            centered
            >
            <Modal.Header>
                <Modal.Title id="contained-modal-title-vcenter">
                    Rename "{documentName}"?
                </Modal.Title>
            </Modal.Header>
            <Modal.Body>
                <p>
                    Please input the new name:
                </p>
                <input
                    id="rename-modal-input"
                    className="rename-modal-input" 
                    placeholder="Enter new name..."
                />
            </Modal.Body>
            <Modal.Footer>
                <Button onClick={() => modalToggler(false)} variant="light">
                    Cancel
                </Button>
                <Button
                    onClick={handleModalConfirm}
                    variant="primary"
                    >
                    Confirm
                </Button>
            </Modal.Footer>
        </Modal>
    );
}