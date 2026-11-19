class ServiceError(Exception):
    """服务层错误：message 一律是给值班看的中文白话。"""

    def __init__(self, message: str, status_code: int = 400, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data or {}
        super().__init__(message)
