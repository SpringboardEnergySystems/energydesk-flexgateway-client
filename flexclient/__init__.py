"""Reference client for the EnergyDesk Flexgateway OpenADR 3.1.0 API."""

from .client import FlexgatewayApiError, FlexgatewayClient, discover_token_url, fetch_access_token
from . import groups, payloads

__version__ = "0.2.0"
__all__ = ["FlexgatewayApiError", "FlexgatewayClient", "discover_token_url", "fetch_access_token", "groups", "payloads"]
