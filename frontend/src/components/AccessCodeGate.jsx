import React, { useCallback, useEffect, useState } from "react";
import Modal from "react-bootstrap/Modal";
import Button from "react-bootstrap/Button";
import Form from "react-bootstrap/Form";
import {
  ACCESS_CODE_REQUIRED_EVENT,
  getAccessCode,
  setAccessCode,
} from "../utilities/apiHeaders";

/**
 * Blocks the app behind a small access-code dialog when none is stored.
 * Re-opens on 401 via the prompthive:access-code-required event.
 */
export default function AccessCodeGate({ children }) {
  const [open, setOpen] = useState(() => !getAccessCode());
  const [value, setValue] = useState("");
  const [message, setMessage] = useState("");

  const showDialog = useCallback((msg = "") => {
    setMessage(msg);
    setValue("");
    setOpen(true);
  }, []);

  useEffect(() => {
    if (!getAccessCode()) {
      showDialog("");
    }
    const onRequired = (event) => {
      showDialog(event?.detail?.message || "Access code incorrect");
    };
    window.addEventListener(ACCESS_CODE_REQUIRED_EVENT, onRequired);
    return () => {
      window.removeEventListener(ACCESS_CODE_REQUIRED_EVENT, onRequired);
    };
  }, [showDialog]);

  const submit = (event) => {
    event.preventDefault();
    const code = value.trim();
    if (!code) {
      return;
    }
    setAccessCode(code);
    setMessage("");
    setOpen(false);
  };

  return (
    <>
      <Modal
        show={open}
        backdrop="static"
        keyboard={false}
        centered
        aria-labelledby="prompthive-access-code-title"
      >
        <Form onSubmit={submit}>
          <Modal.Header>
            <Modal.Title id="prompthive-access-code-title">
              Access code
            </Modal.Title>
          </Modal.Header>
          <Modal.Body>
            {message ? <p className="text-danger mb-2">{message}</p> : null}
            <Form.Group controlId="prompthive-access-code">
              <Form.Label>Access code</Form.Label>
              <Form.Control
                type="password"
                autoComplete="off"
                value={value}
                onChange={(e) => setValue(e.target.value)}
                autoFocus
              />
            </Form.Group>
          </Modal.Body>
          <Modal.Footer>
            <Button type="submit" variant="primary">
              OK
            </Button>
          </Modal.Footer>
        </Form>
      </Modal>
      {children}
    </>
  );
}
