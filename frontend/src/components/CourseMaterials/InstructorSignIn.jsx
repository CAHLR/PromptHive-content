import React, { useState } from "react";
import { Alert, Button, Form } from "react-bootstrap";
import { signIn } from "../../services/courseMaterialsService";

export default function InstructorSignIn({ onSignedIn }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event) {
    event.preventDefault(); setBusy(true); setError("");
    try { await signIn(email, password); setPassword(""); onSignedIn(); }
    catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  return <section className="cm-card cm-sign-in"><h2>Instructor sign-in</h2><p>Use the account and course access provided by your administrator.</p>
    <Form onSubmit={submit}>{error && <Alert variant="danger">{error}</Alert>}
      <Form.Group controlId="instructor-email"><Form.Label>Email</Form.Label><Form.Control type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} /></Form.Group>
      <Form.Group controlId="instructor-password"><Form.Label>Password</Form.Label><Form.Control type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} /></Form.Group>
      <Button type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</Button>
    </Form></section>;
}
