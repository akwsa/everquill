"""Shared gateway exceptions without transport or authentication imports."""


class AuthError(RuntimeError):
    pass


class GatewayError(RuntimeError):
    def __init__(self, status, payload):
        super().__init__(f"gateway status={status} payload={payload!r}")
        self.status = status
        self.payload = payload
