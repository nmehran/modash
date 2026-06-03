from methods.runtime_evaluator.observations import RuntimeSourceObservationError


class RuntimeSourceTraceError(RuntimeSourceObservationError):
    def __init__(self, message: str, code: str = "runtime.trace.invalid"):
        super().__init__(message, code=code)
