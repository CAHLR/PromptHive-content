import React, { useState } from "react";
import { Form } from "react-bootstrap";

export default function LessonPicker({ course, selected, onChange, disabled = false, id = "material-lessons" }) {
  const [search, setSearch] = useState("");
  const lessons = course?.lessons || [];
  const shown = lessons.filter((lesson) => `${lesson.name} ${lesson.topics}`.toLowerCase().includes(search.toLowerCase()));
  return <fieldset className="cm-lessons" disabled={disabled || !course}>
    <legend>Lesson assignment <span className="cm-optional">Optional at upload</span></legend>
    <p className="cm-help">Choose where Oski may use this material. Office Hours includes materials assigned to the course’s lessons.</p>
    {!course ? <div className="cm-lesson-placeholder">Select a course to see its lessons.</div> : <>
      <div className="cm-lesson-tools">
        <Form.Control aria-label="Search lessons" type="search" placeholder="Find a lesson…" value={search} onChange={(e) => setSearch(e.target.value)} />
        <span>{selected.length} selected</span>
      </div>
      <div className="cm-lesson-options">
        {shown.length === 0 && <p className="cm-help">No lessons match your search.</p>}
        {shown.map((lesson, index) => <Form.Check key={lesson.id} id={`${id}-${index}`} type="checkbox"
          checked={selected.includes(lesson.id)} label={<>{lesson.name}{lesson.topics && <small>{lesson.topics}</small>}</>}
          onChange={(e) => onChange(e.target.checked ? [...selected, lesson.id] : selected.filter((item) => item !== lesson.id))} />)}
      </div>
      {selected.length > 0 && <button type="button" className="cm-text-button" onClick={() => onChange([])}>Clear selection</button>}
    </>}
  </fieldset>;
}
