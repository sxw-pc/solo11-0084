"""Exception types used across ReleaseHarbor."""


class ReleaseHarborError(Exception):
    """Base class for expected application errors."""


class ConfigError(ReleaseHarborError):
    """A configuration file is missing, malformed, or unsafe."""


class FixtureError(ReleaseHarborError):
    """The synthetic portal fixture is malformed."""


class SecurityBoundaryError(ReleaseHarborError):
    """A URL or bind address falls outside the local-only boundary."""


class TransportError(ReleaseHarborError):
    """A local HTTP request failed at the transport layer."""


class DiscoveryError(ReleaseHarborError):
    """A portal page could not be interpreted safely."""


class DownloadError(ReleaseHarborError):
    """A transfer failed for a non-retryable protocol reason."""


class AttributionError(ReleaseHarborError):
    """The publisher attribution registry or its analysis inputs are invalid."""


class IntegrityError(ReleaseHarborError):
    """A downloaded artifact failed size or digest acceptance."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
