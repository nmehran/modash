class UnsupportedSourceError(NotImplementedError):
    def __init__(self, message: str | None = None, *, diagnostic=None, code: str | None = None,
                 hint: str | None = None, details: dict | None = None):
        if diagnostic is not None and message is None:
            message = f"{diagnostic.message}: {diagnostic.fragment}"
        super().__init__(message or "unsupported source")
        self.diagnostic = diagnostic
        self.code = diagnostic.code if diagnostic is not None else code
        self.hint = diagnostic.hint if diagnostic is not None else hint
        self.details = diagnostic.details if diagnostic is not None else (details or {})

    def with_diagnostic(self, diagnostic):
        if self.diagnostic is not None:
            return self
        return UnsupportedSourceError(str(self), diagnostic=diagnostic, code=self.code, hint=self.hint)


class FailglobExpansionError(UnsupportedSourceError):
    def __init__(self, pattern: str, source_site: str):
        super().__init__(f"unsupported failglob source pattern: {source_site.strip()}")
        self.pattern = pattern
        self.source_site = source_site
