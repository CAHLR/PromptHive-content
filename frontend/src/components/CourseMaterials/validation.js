export const MAX_PDF_BYTES = 25 * 1024 * 1024;

export function validateFile(file, maxBytes = MAX_PDF_BYTES) {
  if (!file) return "Choose a PDF to upload.";
  if (!file.name.toLowerCase().endsWith(".pdf") || (file.type && file.type !== "application/pdf")) return "Only PDF files are supported.";
  if (!file.size) return "This PDF is empty. Choose another file.";
  if (file.size > maxBytes) return "PDFs must be 25 MB or smaller.";
  return null;
}

export function validateForm(form, file, courses, maxBytes) {
  const errors = {};
  const fileError = validateFile(file, maxBytes);
  if (fileError) errors.file = fileError;
  if (!form.title.trim() || form.title.trim().length > 180) errors.title = "Enter a title of 1–180 characters.";
  if (form.author.trim().length > 180) errors.author = "Author must be 180 characters or fewer.";
  const course = courses.find((item) => item.id === form.courseId);
  if (form.newCourse && form.courseId === "__new__") {
    const name = form.newCourse.courseName.trim();
    const lessons = form.newCourse.lessons.map(name => name.trim());
    if (!name || name.length > 180 || name.startsWith("!!")) errors.newCourse = "Enter a course name of 1–180 characters (not starting with !!).";
    else if (courses.some(c => c.name.trim().toLowerCase() === name.toLowerCase())) errors.newCourse = "This course already exists. Select it from the list.";
    else if (!lessons.length || lessons.length > 50 || lessons.some(name => !name || name.length > 180)) errors.newCourse = "Enter 1–50 lesson names, one per line (up to 180 characters each).";
    else if (new Set(lessons.map(name => name.toLowerCase())).size !== lessons.length) errors.newCourse = "Use a different name for each lesson.";
  } else if (!course) errors.courseId = "Select a course.";
  if (course && form.lessonIds.some((id) => !course.lessons.some((lesson) => lesson.id === id))) errors.lessonIds = "Select lessons from this course.";
  return errors;
}

export function formatFileSize(bytes) {
  return bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.ceil(bytes / 1024))} KB`;
}

export const STATUS_LABELS = {
  cancelled: "Cancelled", uploading: "Awaiting upload", uploaded: "Uploaded", queued: "Queued", extracting: "Extracting",
  compiling: "Compiling", validating: "Validating", awaiting_review: "Awaiting review",
  publish_queued: "Publishing queued", publication_uncertain: "Verification pending", ready_to_publish: "Reviewed · not published", publishing: "Publishing", published: "Published", failed: "Failed",
};
