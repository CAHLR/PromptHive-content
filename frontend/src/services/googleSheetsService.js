import apiClient from "./abscribeAPI";
import {
  normalizeOatutorHeaderRow,
  resolveSheetColumnIndex,
} from "../utilities/utils";

function assertGoogleSheetsResponse(response, action) {
  if (response.statusText !== "OK") {
    throw new Error(`Failed to ${action}: ${response.statusText}`);
  }
  const data = response.data;
  if (
    data &&
    typeof data === "object" &&
    !Array.isArray(data) &&
    data.error
  ) {
    throw new Error(String(data.error));
  }
  return data;
}

export const fetchGoogleSheetsData = async (
  spreadsheetId,
  range,
  filterCriteria
) => {
  try {
    const encodedSpreadsheetId = encodeURIComponent(spreadsheetId);
    const encodedRange = encodeURIComponent(range);
    const response = await apiClient.get(
      `/api/google-sheets/data?spreadsheetId=${encodedSpreadsheetId}&range=${encodedRange}`
    );
    console.log(response);
    let data = assertGoogleSheetsResponse(
      response,
      "fetch data from Google Sheets"
    );

    if (Array.isArray(data) && data.length > 0 && Array.isArray(data[0])) {
      normalizeOatutorHeaderRow(data[0]);
    }

    // Apply filter if filterCriteria is provided
    if (filterCriteria) {
      const columnIndex = resolveSheetColumnIndex(
        data[0],
        filterCriteria.column
      );
      if (columnIndex !== -1) {
        data = data.filter((row) =>
          filterCriteria.values.includes(row[columnIndex])
        );
      } else {
        throw new Error(`Column ${filterCriteria.column} not found.`);
      }
    }

    return data;
  } catch (error) {
    console.error("Error fetching data from Google Sheets:", error);
    throw error;
  }
};

export const fetchGoogleSheetsTitles = async (spreadsheetId) => {
  try {
    const encodedSpreadsheetId = encodeURIComponent(spreadsheetId);
    const response = await apiClient.get(
      `/api/google-sheets/titles?spreadsheetId=${encodedSpreadsheetId}`
    );
    const titles = assertGoogleSheetsResponse(
      response,
      "fetch sheet titles from Google Sheets"
    );
    return titles;
  } catch (error) {
    console.error("Error fetching sheet titles from Google Sheets:", error);
    throw error;
  }
};
export const fetchGoogleSheetsFileName = async (spreadsheetId) => {
  try {
    const encodedSpreadsheetId = encodeURIComponent(spreadsheetId);
    const response = await apiClient.get(
      `/api/google-sheets/file-name?spreadsheetId=${encodedSpreadsheetId}`
    );
    const fileName = assertGoogleSheetsResponse(
      response,
      "fetch Google Sheets file name"
    );
    return fileName;
  } catch (error) {
    console.error("Error fetching Google Sheets file name:", error);
    throw error;
  }
};

export const appendRowsToSheet = async (spreadsheetId, range, values) => {
  try {
    const response = await apiClient.post(`/api/google-sheets/append`, {
      spreadsheetId,
      range,
      values,
    });
    return assertGoogleSheetsResponse(
      response,
      "append rows to Google Sheets"
    );
  } catch (error) {
    console.error("Error appending rows to Google Sheets:", error);
    throw error;
  }
};

export const createNewSheet = async (spreadsheetId, sheetTitle) => {
  try {
    const response = await apiClient.post(`/api/google-sheets/create`, {
      spreadsheetId,
      sheetTitle,
    });
    return assertGoogleSheetsResponse(
      response,
      "create new sheet in Google Sheets"
    );
  } catch (error) {
    if (/already exists/i.test(String(error?.message || ""))) {
      return null;
    }
    console.error("Error creating new sheet in Google Sheets:", error);
    throw error;
  }
};

export const deleteRowsFromSheet = async (
  spreadsheetId,
  sheetName,
  startIndex,
  endIndex
) => {
  try {
    const response = await apiClient.post(`/api/google-sheets/delete-rows`, {
      spreadsheetId,
      sheetName,
      startIndex,
      endIndex,
    });
    return assertGoogleSheetsResponse(
      response,
      "delete rows from Google Sheets"
    );
  } catch (error) {
    console.error("Error deleting rows from Google Sheets:", error);
    throw error;
  }
};

export const updateColumnsInSheet = async (spreadsheetId, range, values) => {
  try {
    const response = await apiClient.post(`/api/google-sheets/update-columns`, {
      spreadsheetId,
      range,
      values,
    });
    return assertGoogleSheetsResponse(
      response,
      "update columns in Google Sheets"
    );
  } catch (error) {
    console.error("Error updating columns in Google Sheets:", error);
    throw error;
  }
};
