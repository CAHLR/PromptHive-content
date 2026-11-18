import Modal from "react-bootstrap/Modal";
import Button from "react-bootstrap/Button";
import { deleteDocument } from "../../../services/documentService";

export default function DeleteListModal({
    show, modalToggler, deleteModeToggler, documentList, listCleaner, documentsFetcher
}) {

    const exitDeleteMode = () => {
        listCleaner()
        deleteModeToggler()
        modalToggler()
    }

    async function deleteDocuments(documents) {
        await Promise.all(documents.map( async (docInfo) => {
            await deleteDocument(docInfo["ID"])
            console.log()
        }));
    }

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
                    Delete {documentList.length} documents?
                </Modal.Title>
            </Modal.Header>
            <Modal.Body>
                <p>
                    You will be deleting the following documents:
                </p>
                {documentList.map((documentInfo) => <p>
                        {documentInfo["name"]}
                    </p>
                )}
                <p>
                    This action is permanent!
                </p>
            </Modal.Body>
            <Modal.Footer>
                <Button onClick={exitDeleteMode} variant="light">
                    Cancel
                </Button>
                <Button
                    onClick={() => {
                        deleteDocuments(documentList).then(
                            () => {
                                console.log("Documents deleted in backend");
                                documentsFetcher()
                            }).catch(
                                (error) =>
                                console.error("Failed to delete documents in backend:", error)
                            ).finally(() => {
                                exitDeleteMode();
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