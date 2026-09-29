export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const body = await response.json();
      message = typeof body.detail === "string" ? body.detail : Array.isArray(body.detail)
        ? body.detail.map((issue: { loc?: string[]; msg?: string }) => `${issue.loc?.slice(1).join(".") || "输入"}：${issue.msg || "格式不正确"}`).join("；")
        : message;
    } catch {
      // Keep the generic message for non-JSON errors.
    }
    throw new ApiError(response.status, message);
  }
  return response.json() as Promise<T>;
}
