from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError


class ChatError(RuntimeError):
    def __init__(
        self, code: str, message: str, status: int, retryable: bool = False, *, diagnostics=None
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.thread_id: str | None = None
        self.request_id: str | None = None
        self.diagnostics = diagnostics

    def detail(self):
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "thread_id": self.thread_id,
            "request_id": self.request_id,
            **({"diagnostics": self.diagnostics} if self.diagnostics else {}),
        }


def truncated_response():
    return ChatError(
        "model_response_truncated", "模型回答达到输出长度上限，请调整输出预算后重新提问。", 502
    )


def upstream_error(error: APIError) -> ChatError:
    if isinstance(error, APITimeoutError):
        return ChatError("model_timeout", "模型 API 请求超时，可稍后再次提问。", 504, True)
    if isinstance(error, APIConnectionError):
        return ChatError("upstream_connection_error", "无法连接模型服务。", 502, True)
    if isinstance(error, APIStatusError):
        status = error.status_code
        if status in (401, 403):
            return ChatError("upstream_auth_error", "模型服务鉴权失败，请检查凭据或权限。", 502)
        if status == 429:
            return ChatError(
                "upstream_rate_limited", "模型服务限流或额度不足，请检查后重试。", 503, True
            )
        if status >= 500:
            return ChatError("upstream_unavailable", "模型服务暂时不可用。", 502, True)
        return ChatError("upstream_request_error", "模型服务拒绝请求，请检查模型与参数。", 502)
    return ChatError("upstream_error", "模型 API 调用失败。", 502, True)
