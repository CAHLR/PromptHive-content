import React, { useState } from "react";
import {
  createNewSheet,
  appendRowsToSheet,
} from "../../services/googleSheetsService";
import {
  buildSheetExportMatrix,
  getVisibleSheetRowsForExport,
} from "../../utilities/sheetExportUtils";
import { Button, Alert } from "react-bootstrap";

const HintExporter = ({
  lessonContentAttributes,
  columnIndexMap,
  sheetInfo,
  showHumanHints = false,
}) => {
  const [outputSheetURL, setOutputSheetURL] = useState(
    "https://docs.google.com/spreadsheets/d/1Jbrh3WBHgiuWkuv1aXa2NMKkMVK8CpDhMjep1R8qNeA/edit?gid=1513559323#gid=1513559323"
  );
  const [exporting, setExporting] = useState(false);
  const [statusMessage, setStatusMessage] = useState(null);
  const [statusVariant, setStatusVariant] = useState("danger");
  const [outputSpreadsheetId, setOutputSpreadsheetId] = useState(
    "1Jbrh3WBHgiuWkuv1aXa2NMKkMVK8CpDhMjep1R8qNeA"
  );
  const [outputSheetName, setOutputSheetName] = useState("ExportedHints");

  const extractSheetIdFromUrl = (url) => {
    const regex = /\/d\/([a-zA-Z0-9-_]+)/;
    const match = url.match(regex);
    return match ? match[1] : null;
  };

  const handleOutputSheetUrlChange = (e) => {
    const url = e.target.value;
    setOutputSheetURL(url);
    setOutputSpreadsheetId(extractSheetIdFromUrl(url) || "");
  };

  const handleOutputSheetNameChange = (e) => {
    setOutputSheetName(e.target.value);
  };

  async function createAndAppendRows(spreadsheetId, sheetData, sheetHeaders) {
    for (const sheetName in sheetData) {
      if (!Object.prototype.hasOwnProperty.call(sheetData, sheetName)) {
        continue;
      }

      const rows = sheetData[sheetName];
      const headers = sheetHeaders[sheetName] || [];

      await createNewSheet(spreadsheetId, sheetName);

      if (headers.length > 0) {
        const headerRange = `${sheetName}!A1`;
        await appendRowsToSheet(spreadsheetId, headerRange, [headers]);
      }

      if (rows.length > 0) {
        const dataRange = `${sheetName}!A2`;
        await appendRowsToSheet(spreadsheetId, dataRange, rows);
      }
    }
  }

  const exportToSheet = async () => {
    if (!outputSpreadsheetId) {
      setStatusVariant("danger");
      setStatusMessage("Enter a valid output Google Sheet URL.");
      return;
    }

    if (!columnIndexMap || !lessonContentAttributes) {
      setStatusVariant("danger");
      setStatusMessage("Sheet columns are not loaded yet. Reload the lesson.");
      return;
    }

    const visibleRows = getVisibleSheetRowsForExport(sheetInfo, {
      showHumanHints,
    });
    const { headers, rows } = buildSheetExportMatrix(
      visibleRows,
      columnIndexMap,
      lessonContentAttributes
    );

    if (!rows.length) {
      setStatusVariant("danger");
      setStatusMessage("No visible questions to export.");
      return;
    }

    setExporting(true);
    setStatusMessage(null);

    const sheetHeaders = {
      [outputSheetName]: headers,
    };
    const sheetData = {
      [outputSheetName]: rows,
    };

    try {
      await createAndAppendRows(outputSpreadsheetId, sheetData, sheetHeaders);
      setStatusVariant("success");
      setStatusMessage(
        `Exported ${rows.length} row(s) to "${outputSheetName}".`
      );
    } catch (error) {
      setStatusVariant("danger");
      setStatusMessage(
        error?.message || "Export failed. Check the output sheet URL and permissions."
      );
    } finally {
      setExporting(false);
    }
  };

  return (
    <>
      <div>
        <label htmlFor="outputSheetURL">Output Sheet URL:</label>
        <input
          type="text"
          id="outputSheetURL"
          value={outputSheetURL}
          onChange={handleOutputSheetUrlChange}
        />
      </div>
      <div>
        <label htmlFor="outputSheetName">Output Sheet Name:</label>
        <input
          type="text"
          id="outputSheetName"
          value={outputSheetName}
          onChange={handleOutputSheetNameChange}
        />
      </div>
      <Button onClick={exportToSheet} disabled={exporting}>
        {exporting ? "Exporting..." : "Export to Output Sheet"}
      </Button>
      {statusMessage && <Alert variant={statusVariant}>{statusMessage}</Alert>}
    </>
  );
};

export default HintExporter;
