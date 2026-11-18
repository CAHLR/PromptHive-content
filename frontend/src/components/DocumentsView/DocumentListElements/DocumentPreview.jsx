import { useState, useEffect } from "react"
import { Dropdown, ListGroup } from "react-bootstrap";
import DropdownItem from "react-bootstrap/DropdownItem";
import Container from "react-bootstrap/Container";
import RenameModal from "./RenameModal.jsx";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import Sugar from "sugar";
import { faFile, faEllipsisV } from "@fortawesome/free-solid-svg-icons";

export default function DocumentPreview({
    document: file, clickHandler, deleteHandler, deleteMode
}) {
    const [fileName, setFileName] = useState(file.name)

    const [renameModalShow, setRenameModalShow] = useState(false);
    const [renameModalName, setRenameModalName] = useState("");
    const [renameModalId, setRenameModalId] = useState("");
    const [renameModalContent, setRenameModalContent] = useState("");

    useEffect(() => setFileName(file.name), [file])

    const getFormattedTimestamp = (timestamp) => {
        var date = new Date(timestamp + "Z"); //appending Z so considered valid ISO 8601
        return Sugar.Date(date).relative();
    };

    const handleClick = () => {
        if (deleteMode) {
            const checkboxElement = document.querySelector(`#checkbox${file._id}`)
            checkboxElement.checked = !checkboxElement.checked
        }
        clickHandler(file._id, fileName)
    }

    const handleRenameDocumentClick = (documentId, documentName, documentContent) => {
        setRenameModalShow(true);
        setRenameModalName(documentName);
        setRenameModalId(documentId);
        setRenameModalContent(documentContent)
    }

    return <>
    <ListGroup.Item
        key={file._id}
        style={{
            cursor: "pointer",
        }}
        >
        <Container 
            style={{
                display: "flex"
            }}
            className={
                `document${deleteMode ? " delete-mode-document" : ""}`
            }
            >
            <div
                style={{
                    display: "flex",
                    flexDirection: "row",
                    gap: "2em",
                    alignItems: "center",
                    flex: 1
                }}
                className="text-truncate"
                onClick={handleClick}
                >
                <FontAwesomeIcon icon={faFile} />

                {fileName}
            </div>

            <div
                style={{
                    display: "flex",
                    flexDirection: "row",
                    alignItems: "center"
                }}
                >
                <small 
                    className="m-1 p-2 text-muted"
                    onClick={handleClick}
                    >
                    {`Last Modified: ${getFormattedTimestamp(file.last_modified_date)}`}
                </small>
                <div
                    style={{
                        display: "flex",
                        justifyContent: "center",
                        width: "2em"
                    }}
                    >
                    {
                        deleteMode ?
                        
                        <input
                            id={`checkbox${file._id}`}
                            style={{
                                cursor: "pointer"
                            }}
                            type="checkbox"
                            onClick={(event) => {
                                event.preventDefault();
                                handleClick()
                            }}/> :

                        <Dropdown
                            style={{
                                boxShadow: "none",
                            }}
                            >
                            <Dropdown.Toggle variant="light">
                                {<FontAwesomeIcon icon={faEllipsisV} />}
                            </Dropdown.Toggle>

                            <Dropdown.Menu>
                                <DropdownItem
                                    onClick={() => deleteHandler(file._id, fileName)}
                                    >
                                    <small>
                                        Delete Document
                                    </small>
                                </DropdownItem>
                            
                                <DropdownItem
                                    onClick={() => handleRenameDocumentClick(
                                        file._id, fileName, file.content
                                    )}
                                    >
                                    <small>
                                        Rename Document
                                    </small>
                                </DropdownItem>
                            </Dropdown.Menu>
                        </Dropdown>
                    }
                </div>
            </div>
        </Container>
    </ListGroup.Item>

    <RenameModal
        show={renameModalShow}
        modalToggler={() => setRenameModalShow(false)}
        documentId={renameModalId}
        documentName={renameModalName}
        documentContent={renameModalContent}
        documentNameSetter={setFileName}
        />
</>}