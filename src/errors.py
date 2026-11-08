"""All pipeline exceptions in one module (re-exported by the modules that raise them)."""


class PipelineError(Exception):
    exit_code = 1


class ConfigError(PipelineError):
    exit_code = 2


class RetryExhausted(PipelineError):
    exit_code = 3

    def __init__(self, message, url=None, attempts=None, last_cause=None):
        super().__init__(message)
        self.url = url
        self.attempts = attempts
        self.last_cause = last_cause


class RateLimitExhausted(RetryExhausted):
    exit_code = 3


class ApiHttpError(PipelineError):
    exit_code = 3


class SchemaCheckError(PipelineError):
    exit_code = 4

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = list(errors or [])


class RowCountFailure(PipelineError):
    exit_code = 5

    def __init__(self, message, failed=None):
        super().__init__(message)
        self.failed = list(failed or [])


class StagingError(PipelineError):
    exit_code = 6

    def __init__(self, message, request_ids=None):
        super().__init__(message)
        self.request_ids = list(request_ids or [])


class MergeError(PipelineError):
    exit_code = 7


class FileCollisionError(PipelineError):
    exit_code = 8


class AnalysisError(PipelineError):
    exit_code = 9
