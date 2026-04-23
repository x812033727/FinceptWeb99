import axios from "axios";

const api = axios.create({
  baseURL: "/api",
  withCredentials: true,
});

// Phase 1 will add: request interceptor (attach Bearer token)
// and response interceptor (auto-refresh on 401)

export default api;
