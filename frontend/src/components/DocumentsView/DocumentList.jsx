import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import ListGroup from "react-bootstrap/ListGroup";
import Container from "react-bootstrap/Container";
import Header from "./DocumentListElements/Header.jsx";
import DocumentPreview from "./DocumentListElements/DocumentPreview.jsx";
import DeleteConfirmationModal from "./DocumentListElements/DeleteConfirmationModal.jsx";
import DeleteListModal from "./DocumentListElements/DeleteListModal.jsx";
import NavHeader from "../Header/NavHeader";
import { getDocuments } from "../../services/documentService";
import "../../scss/documentlist.scss";

const DocumentList = () => {
  const [query, setQuery] = useState("");
  const [filter, setfilter] = useState("");
  const [documents, setDocuments] = useState([]);

  // Delete mode is still buggy! Can't select more than one doc...
  const [deleteMode, setDeleteMode] = useState(false);
  const [deleteModeList, setDeleteModeList] = useState([]);
  const [deleteListModalShow, setDeleteListModalShow] = useState(false);

  const [deleteModalShow, setDeleteModalShow] = useState(false);
  const [deleteModalName, setDeleteModalName] = useState("");
  const [deleteModalId, setDeleteModalId] = useState("");

  const navigate = useNavigate();

  useEffect(() => {
    fetchDocuments();
  }, [query, filter]);

  const fetchDocuments = async () => {
    const fetchedDocuments = await getDocuments(query, filter);
    setDocuments(fetchedDocuments);

    console.log("Fetched documents: ", fetchedDocuments);
  };

  const handleDocumentClick = (documentId, documentName) => {
    console.log("Document clicked: ", document);
    if (deleteMode) {
      const docInfo = {
        ID: documentId,
        name: documentName,
      };

      let similarDoc = null;

      for (const doc of deleteModeList) {
        if (docInfo["ID"] == doc["ID"]) {
          similarDoc = doc;
          break;
        }
      }

      let newDeleteList = deleteModeList;

      if (similarDoc) {
        const index = newDeleteList.indexOf(similarDoc);
        newDeleteList.splice(index, 1);
      } else {
        newDeleteList.push(docInfo);
      }

      setDeleteModeList(newDeleteList);
    } else {
      navigate(`/document/${documentId}`);
    }
  };

  const handleDeleteDocumentClick = (documentId, documentName) => {
    setDeleteModalShow(true);
    setDeleteModalName(documentName);
    setDeleteModalId(documentId);
  };

  const HorizontalRule = ({ text }) => {
    return (
      <>
        <div className="text-truncate document-separator">{text}</div>

        <hr />
      </>
    );
  };

  return (
    <>
      <NavHeader />
      <Container className="p-3">
        <ListGroup variant="flush">
          <Header
            querySetter={setQuery}
            filter={filter}
            filterSetter={setfilter}
            deleteModeToggler={setDeleteMode}
            deleteMode={deleteMode}
            deleteModeList={deleteModeList}
            deleteModeHandler={() => setDeleteListModalShow(true)}
          />
          {documents.map((document) =>
            typeof document === "string" ? (
              <HorizontalRule text={document} />
            ) : (
              <DocumentPreview
                document={document}
                clickHandler={handleDocumentClick}
                deleteHandler={handleDeleteDocumentClick}
                deleteMode={deleteMode}
                deleteModeList={deleteModeList}
                deleteModeListSetter={setDeleteModeList}
              />
            )
          )}
        </ListGroup>

        <DeleteConfirmationModal
          show={deleteModalShow}
          modalToggler={() => setDeleteModalShow(false)}
          documentId={deleteModalId}
          documentName={deleteModalName}
          documentsFetcher={fetchDocuments}
        />

        <DeleteListModal
          show={deleteListModalShow}
          modalToggler={() => setDeleteListModalShow(false)}
          deleteModeToggler={() => setDeleteMode(false)}
          documentList={deleteModeList}
          listCleaner={() => setDeleteModeList([])}
          documentsFetcher={fetchDocuments}
        />
      </Container>
    </>
  );
};

export default DocumentList;
