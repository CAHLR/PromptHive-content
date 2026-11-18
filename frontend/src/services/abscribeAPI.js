import axios from "axios";
import { API_BASE_URL } from "../config";
import {
  apiHeaders,
  handleAccessCodeUnauthorized,
} from "../utilities/apiHeaders";

const apiClient = axios.create({
  baseURL: API_BASE_URL, // Replace with your API's base URL
  headers: {
    "Content-Type": "application/json",
  },
});

apiClient.interceptors.request.use((config) => {
  const extras = apiHeaders();
  config.headers = {
    ...(config.headers || {}),
    ...extras,
  };
  return config;
});

apiClient.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error?.response?.status === 401) {
      handleAccessCodeUnauthorized();
    }
    return Promise.reject(error);
  }
);

export default apiClient;
