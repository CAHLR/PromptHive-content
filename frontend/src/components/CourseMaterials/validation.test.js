import { test } from "node:test";
import assert from "node:assert/strict";
import { MAX_PDF_BYTES, validateFile, validateForm, formatFileSize } from "./validation.js";

test("PDF validation rejects empty, oversized and disguised files", () => {
  assert.ok(validateFile(null));
  assert.ok(validateFile({ name: "a.pdf", type: "application/pdf", size: 0 }));
  assert.ok(validateFile({ name: "a.pdf", type: "text/plain", size: 20 }));
  assert.ok(validateFile({ name: "a.exe", type: "application/pdf", size: 20 }));
  assert.ok(validateFile({ name: "a.pdf", type: "application/pdf", size: MAX_PDF_BYTES + 1 }));
  assert.equal(validateFile({ name: "a.PDF", type: "", size: MAX_PDF_BYTES }), null);
});
test("form requires title and real course with valid lesson assignments", () => {
  const file = { name: "a.pdf", type: "application/pdf", size: 20 };
  const courses = [{ id: "a", lessons: [{ id: "lesson-a" }] }];
  const form = { title: "Reference", author: "", courseId: "a", lessonIds: ["lesson-a"] };
  assert.deepEqual(validateForm(form, file, courses), {});
  assert.ok(validateForm({ ...form, title: " " }, file, courses).title);
  assert.ok(validateForm({ ...form, author: "a".repeat(181) }, file, courses).author);
  assert.ok(validateForm({ ...form, courseId: "fake" }, file, courses).courseId);
  assert.ok(validateForm({ ...form, lessonIds: ["other-course"] }, file, courses).lessonIds);
  assert.deepEqual(validateForm({ ...form, lessonIds: [] }, file, courses), {});
});
test("file size has readable units", () => {
  assert.equal(formatFileSize(2000), "2 KB");
  assert.equal(formatFileSize(5 * 1024 * 1024), "5.0 MB");
});

test("new course requires unique course and lesson names", () => {
  const file = { name: "a.pdf", type: "application/pdf", size: 20 };
  const form = { title: "Reference", author: "", courseId: "__new__", lessonIds: [], newCourse: { courseName: "Biology", lessons: ["Cells", "Ecology"] } };
  assert.deepEqual(validateForm(form, file, []), {});
  assert.ok(validateForm(form, file, [{name: " biology "}]).newCourse);
  for (const lessons of [[], [""], ["Cells", "cells"], Array(51).fill("Lesson")]) {
    assert.ok(validateForm({...form, newCourse: {...form.newCourse, lessons}}, file, []).newCourse);
  }
});
