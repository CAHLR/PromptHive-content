import React from "react";
import Navbar from "react-bootstrap/Navbar";
import Container from "react-bootstrap/Container";
import Nav from "react-bootstrap/Nav";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faFilePen } from "@fortawesome/free-solid-svg-icons";
import Button from "react-bootstrap/Button";
import { Link, NavLink } from "react-router-dom";

export default function NavHeader() {
  return (
    <Navbar bg="light" expand="sm" sticky="top">
      <Container>
        <div className="d-flex align-items-center">
          <Button as={Link} to="/" variant="light">
            <FontAwesomeIcon icon={faFilePen} /> PromptHive
          </Button>
        </div>
        <Navbar.Toggle aria-controls="basic-navbar-nav" />
        <Navbar.Collapse id="basic-navbar-nav">
          <Nav className="me-auto">
            <Nav.Link as={NavLink} to="/" end>Authoring</Nav.Link>
            <Nav.Link as={NavLink} to="/course-materials">Course materials</Nav.Link>
            <Nav.Link as={NavLink} to="/documentlist">Editable documents</Nav.Link>
          </Nav>
          <Nav></Nav>
        </Navbar.Collapse>
      </Container>
    </Navbar>
  );
}
