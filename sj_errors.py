"""Custom exceptions for the SJ API client."""


class SJAPIError(Exception):
    """Exception raised when the SJ API returns an error in the JSON body."""



class SJAuthError(Exception):
    """Exception raised when authentication fails."""



class SJConfigError(Exception):
    """Exception raised when configuration validation fails."""

