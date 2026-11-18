import { useNavigate } from "react-router-dom";
import { ListGroup } from "react-bootstrap";
import Button from "react-bootstrap/Button";
import DropdownItem from 'react-bootstrap/DropdownItem'
import DropdownButton from 'react-bootstrap/DropdownButton';
import Dropdown from 'react-bootstrap/Dropdown';
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faPlus, faCheck, faTrashAlt, faFilter } from "@fortawesome/free-solid-svg-icons";
import { createDocument } from "../../../services/documentService";


export default function Header({
    querySetter, filter, filterSetter, deleteModeToggler, deleteMode, deleteModeList, deleteModeHandler
}) {

    const navigate = useNavigate();

    const handleNewDocumentClick = () => {
      createDocument("","")
        .then((document) => {
          console.log("Document created in backend");
          navigate(`/document/${document._id["$oid"]}`);
        })
        .catch((error) =>
          console.error("Failed to create document in backend:", error)
        );
    };

    const handleDeleteModeClick = () => {
      if (!deleteMode || deleteModeList.length == 0) {
        deleteModeToggler(!deleteMode)
      }
      else {
        console.log(deleteModeList)
        deleteModeHandler()
      }
    }

    const handleKeyUp = (event) => {
      if (event.key === "Enter") {
        querySetter(event.target.value)
        event.target.value = ""
      }
    }

    return (
    <ListGroup.Item
      className="d-flex justify-content-between align-items-center doc-header"
      >
      <input 
        className="search-bar" 
        placeholder="Enter your document name..."
        onKeyUp={handleKeyUp}
        disabled={deleteMode}
      />
      <div
        className="header-button-container"
        >
        <DropdownButton
          style={{
            boxShadow: "none"
          }}
          variant="light"
          aria-label="Filter Documents by Date"
          title={<FontAwesomeIcon icon={faFilter} />}
          disabled={deleteMode}
          >
          {["Today", "Last Week", "Last Month", "Earlier"].map((date) => (
            <DropdownItem
              onClick={() => filterSetter(date)}
              disabled={filter === date}
              >
              <span
                style={{
                  display: "flex",
                  flexDirection: "row",
                  justifyContent: "space-between"
                }}
                >
                {date}
                
                {filter === date ? <FontAwesomeIcon icon={faCheck} />: ""}
              </span>
            </DropdownItem>
          ))}

          <Dropdown.Divider />

          <DropdownItem
            onClick={() => filterSetter("")}
            >
            Clear Filters
          </DropdownItem>
        </DropdownButton>

        <Button
          variant="light"
          aria-label="Delete Documents"
          className={
            `delete-button ${deleteMode ? "delete-mode-button" : ""}`
          }
          onClick={handleDeleteModeClick}
          >
          <FontAwesomeIcon icon={faTrashAlt} />
        </Button>

        <Button
          variant="light"
          aria-label="Create Document"
          onClick={handleNewDocumentClick}
          disabled={deleteMode}
          >
          <FontAwesomeIcon icon={faPlus} />
        </Button>
      </div>  
    </ListGroup.Item>
    )
}