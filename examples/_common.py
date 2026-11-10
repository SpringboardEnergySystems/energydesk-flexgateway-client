"""What every example below does first: find the VTN, get a token, open a client. Kept apart so each example shows only its own point."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from a checkout, no install needed

from dotenv import load_dotenv

from flexclient import FlexgatewayClient, discover_token_url, fetch_access_token


def _token(client_id_var: str, client_secret_var: str, ready_var: str, base_url: str) -> str:
    ready = os.getenv(ready_var)
    if ready:
        return ready
    return fetch_access_token(
        os.getenv("OAUTHCHECK_ACCESS_TOKEN_OBTAIN_URL") or discover_token_url(base_url),
        os.environ[client_id_var], os.environ[client_secret_var], scope=os.getenv("OAUTH_SCOPE") or None)


def partner_client() -> FlexgatewayClient:
    """A client for your VEN: a ready ``FLEXGATEWAY_ACCESS_TOKEN``, or the OAuth client credentials Springboard issued you."""
    load_dotenv()
    base_url = os.environ["FLEXGATEWAY_BASE_URL"]
    return FlexgatewayClient(base_url, _token("OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "FLEXGATEWAY_ACCESS_TOKEN", base_url),
                             ven_name=os.getenv("FLEXGATEWAY_VEN_NAME"), vtn_api_url=os.getenv("FLEXGATEWAY_VTN_API_URL"))
