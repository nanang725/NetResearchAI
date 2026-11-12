import axios, { AxiosError } from "axios";

export interface ApiErrorPayload {
  code: string;
  message: string;
  details?: unknown;
}

export class ApiError extends Error {
  code: string;
  status: number;
  details?: unknown;

  constructor(message: string, code = "REQUEST_FAILED", status = 500, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
    this.details = details;
  }
}

interface Envelope<T> {
  success: boolean;
  data: T | null;
  error: ApiErrorPayload | null;
}

export const http = axios.create({
  baseURL: "/api",
  timeout: 60_000,
});

http.interceptors.response.use(
  (response) => {
    const payload = response.data as Envelope<unknown>;
    if (payload && typeof payload === "object" && "success" in payload) {
      if (!payload.success) {
        const error = payload.error;
        throw new ApiError(
          error?.message || "请求失败",
          error?.code || "REQUEST_FAILED",
          response.status,
          error?.details,
        );
      }
      return {
        ...response,
        data: payload.data,
      };
    }
    return response;
  },
  (error: AxiosError<Envelope<unknown>>) => {
    if (error.response?.data?.error) {
      const payload = error.response.data.error;
      throw new ApiError(
        payload.message,
        payload.code,
        error.response.status,
        payload.details,
      );
    }
    if (error.code === "ERR_NETWORK") {
      throw new ApiError("无法连接后端服务，请确认 FastAPI 已启动。", "NETWORK_ERROR", 0);
    }
    throw new ApiError(error.message || "网络请求失败", "REQUEST_FAILED", error.response?.status || 500);
  },
);
