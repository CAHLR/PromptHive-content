// Backend API base URL.
//
// Resolution order:
//   1. VITE_REACT_APP_API_BASE_URL  (set in Netlify env vars, or frontend/.env.local
//      for local dev — this is the preferred way; no code change needed per env)
//   2. The fallback string below.
//
// For a production build, EITHER set VITE_REACT_APP_API_BASE_URL in Netlify's
// build environment, OR replace BACKEND_URL_HERE with your deployed backend URL
// (e.g. 'https://api.your-domain.edu' or 'https://<server-ip>:8081').
export const API_BASE_URL =
  import.meta.env.VITE_REACT_APP_API_BASE_URL || 'BACKEND_URL_HERE';
