"""Validate requests and responses against the vendored OpenADR 3.1.0 YAML (openapi-core).

The YAML is used as published, with three documented patches (they remove defects of the published file; the full
explanation is in the flexgateway service's ``tests/contract.py`` and ``openadr/spec/README.md``):

* ``servers`` is the test server, and ``security`` is dropped (scopes are the server's business);
* ``ven`` and ``resource`` are ``allOf [objectMetadata, BlVenRequest / BlResourceRequest]`` whose two parts demand different
  ``objectType`` enums, so no object can satisfy them: the request part loses its ``objectType``;
* discriminators without a ``mapping`` cannot be resolved by a strict validator: the mapping is derived from the schemas.
"""
from __future__ import annotations

import copy
import json
import warnings
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import yaml

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from openapi_core import OpenAPI
    from openapi_core.testing import MockRequest, MockResponse

SPEC_PATH = Path(__file__).parent / "spec" / "openadr3-3.1.0.yaml"
BASE = "http://vtn.test"
PREFIX = "/openadr3"


def _complete_discriminators(document: dict) -> None:
    schemas = document["components"]["schemas"]

    def values_of(name: str, prop: str) -> list:
        for part in schemas[name].get("allOf", [schemas[name]]):
            part = schemas[part["$ref"].split("/")[-1]] if "$ref" in part else part
            declared = part.get("properties", {}).get(prop, {})
            if "$ref" in declared:
                declared = schemas[declared["$ref"].split("/")[-1]]
            if declared.get("enum"):
                return declared["enum"]
        return []

    def walk(node):
        if isinstance(node, dict):
            discriminator, options = node.get("discriminator"), node.get("oneOf") or node.get("anyOf")
            if isinstance(discriminator, dict) and "mapping" not in discriminator and options:
                mapping = {}
                for option in options:
                    name = option["$ref"].split("/")[-1]
                    enum = values_of(name, discriminator["propertyName"])
                    if len(enum) == 1:
                        mapping[enum[0]] = option["$ref"]
                    elif name.upper() in enum:
                        mapping[name.upper()] = option["$ref"]
                discriminator["mapping"] = mapping
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(document)


def contract_spec() -> dict:
    document = yaml.safe_load(SPEC_PATH.read_text())
    document["servers"] = [{"url": BASE + PREFIX}]
    for item in document["paths"].values():
        for operation in item.values():
            if isinstance(operation, dict):
                operation.pop("security", None)
    schemas = document["components"]["schemas"]
    for name, request_name in (("ven", "BlVenRequest"), ("resource", "BlResourceRequest")):
        part = copy.deepcopy(schemas[request_name])
        part["properties"].pop("objectType")
        part["required"] = [r for r in part["required"] if r != "objectType"]
        schemas[name] = {"type": "object", "allOf": [{"$ref": "#/components/schemas/objectMetadata"}, part]}
    _complete_discriminators(document)
    return document


def validate_component(name: str, instance) -> None:
    from openapi_schema_validator import OAS30Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT4

    registry = Registry().with_resource("urn:openadr", Resource.from_contents(contract_spec(), default_specification=DRAFT4))
    OAS30Validator({"$ref": f"urn:openadr#/components/schemas/{name}"}, registry=registry).validate(instance)


class Contract:
    def __init__(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.api = OpenAPI.from_dict(contract_spec())

    def request(self, method: str, url: str, body: bytes = b""):
        parts = urlsplit(url)
        return MockRequest(
            f"{parts.scheme}://{parts.netloc}", method.lower(), parts.path, args=dict(parse_qsl(parts.query)),
            headers={}, data=body, content_type="application/json")

    def check_request(self, method: str, url: str, body: bytes = b"") -> None:
        self.api.validate_request(self.request(method, url, body))

    def check_response(self, method: str, url: str, status: int, payload, body: bytes = b"") -> None:
        response = MockResponse(json.dumps(payload).encode(), status_code=status, content_type="application/json")
        self.api.validate_response(self.request(method, url, body), response)
