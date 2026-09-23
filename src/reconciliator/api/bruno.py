"""Dynamic Bruno API test suite generator and exporter."""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections import defaultdict
from typing import Any, Optional

from fastapi import FastAPI

from reconciliator.config import BrunoConfig


def slugify(text: str) -> str:
    """Convert arbitrary text into a URL/filename-safe kebab-case slug."""
    text = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[-\s]+", "-", text) or "endpoint"


class BrunoCollectionGenerator:
    """Introspects OpenAPI 3.x specifications and generates dynamic Bruno test suites."""

    def __init__(
        self,
        openapi_spec: dict[str, Any],
        config: Optional[BrunoConfig] = None,
    ) -> None:
        self.spec = openapi_spec
        self.config = config or BrunoConfig()
        self.components = self.spec.get("components", {}).get("schemas", {})

    def build_bruno_json(self) -> dict[str, Any]:
        """Generate root bruno.json collection manifest."""
        return {
            "version": self.config.version,
            "name": self.config.collection_name,
            "type": "collection",
            "ignore": [
                "node_modules",
                ".git",
            ],
        }

    def build_environment_bru(self) -> str:
        """Generate environments/<env>.bru environment variable declaration."""
        return f"""vars {{
  baseUrl: {self.config.base_url}
}}
"""

    def generate_sample_payload(self, schema_or_ref: dict[str, Any], depth: int = 0) -> Any:
        """Generate a sensible dummy/example payload from an OpenAPI schema object."""
        if depth > 5:
            return {}

        if "$ref" in schema_or_ref:
            ref_name = schema_or_ref["$ref"].split("/")[-1]
            target_schema = self.components.get(ref_name, {})
            return self.generate_sample_payload(target_schema, depth + 1)

        schema_type = schema_or_ref.get("type")
        if schema_type == "object" or "properties" in schema_or_ref:
            result = {}
            for prop_name, prop_schema in schema_or_ref.get("properties", {}).items():
                result[prop_name] = self.generate_sample_payload(prop_schema, depth + 1)
            return result
        elif schema_type == "array":
            items_schema = schema_or_ref.get("items", {})
            return [self.generate_sample_payload(items_schema, depth + 1)]
        elif schema_type == "string":
            if "example" in schema_or_ref:
                return schema_or_ref["example"]
            if "default" in schema_or_ref:
                return schema_or_ref["default"]
            if schema_or_ref.get("format") == "date-time":
                return "2026-09-22T12:00:00Z"
            return "sample_string"
        elif schema_type == "integer":
            return schema_or_ref.get("default", schema_or_ref.get("example", 1))
        elif schema_type == "number":
            return schema_or_ref.get("default", schema_or_ref.get("example", 1.0))
        elif schema_type == "boolean":
            return schema_or_ref.get("default", schema_or_ref.get("example", True))

        if "example" in schema_or_ref:
            return schema_or_ref["example"]
        if "default" in schema_or_ref:
            return schema_or_ref["default"]
        return {}

    def build_bru_file(
        self,
        method: str,
        path: str,
        operation: dict[str, Any],
        sequence: int = 1,
    ) -> str:
        """Construct the plaintext content of a .bru request file with automated assertions."""
        method_lower = method.lower()
        summary = (
            operation.get("summary") or operation.get("operationId") or f"{method.upper()} {path}"
        )
        name = summary.replace("\n", " ").strip()

        # Parse parameters
        query_params: list[tuple[str, str]] = []
        path_params: list[tuple[str, str]] = []
        headers: list[tuple[str, str]] = []

        for param in operation.get("parameters", []):
            param_name = param.get("name")
            param_in = param.get("in")
            schema = param.get("schema", {})
            sample_val = str(
                schema.get(
                    "default",
                    schema.get("example", "1" if schema.get("type") == "integer" else "test"),
                )
            )

            if param_in == "query":
                query_params.append((param_name, sample_val))
            elif param_in == "path":
                path_params.append((param_name, sample_val))
            elif param_in == "header":
                headers.append((param_name, sample_val))

        # Check request body
        request_body = operation.get("requestBody", {})
        content = request_body.get("content", {})
        has_json_body = "application/json" in content
        body_type = "json" if has_json_body else "none"

        sample_body_json: Optional[str] = None
        if has_json_body:
            body_schema = content["application/json"].get("schema", {})
            sample_dict = self.generate_sample_payload(body_schema)
            sample_body_json = json.dumps(sample_dict, indent=2, ensure_ascii=False)
            headers.append(("Content-Type", "application/json"))

        # Determine expected status code
        responses = operation.get("responses", {})
        expected_status = 200
        for code in ["200", "201", "202", "204"]:
            if code in responses:
                expected_status = int(code)
                break

        # Assemble .bru blocks
        lines: list[str] = []

        # meta block
        lines.append("meta {")
        lines.append(f"  name: {name}")
        lines.append("  type: http")
        lines.append(f"  seq: {sequence}")
        lines.append("}\n")

        # http method block
        # Bruno uses :param_name for path parameters
        bruno_url = path
        for p_name, _ in path_params:
            bruno_url = bruno_url.replace(f"{{{p_name}}}", f":{p_name}")

        lines.append(f"{method_lower} {{")
        lines.append(f"  url: {{{{baseUrl}}}}{bruno_url}")
        lines.append(f"  body: {body_type}")
        lines.append("  auth: none")
        lines.append("}\n")

        # params:path block
        if path_params:
            lines.append("params:path {")
            for p_name, p_val in path_params:
                lines.append(f"  {p_name}: {p_val}")
            lines.append("}\n")

        # params:query block
        if query_params:
            lines.append("params:query {")
            for q_name, q_val in query_params:
                lines.append(f"  {q_name}: {q_val}")
            lines.append("}\n")

        # headers block
        if headers:
            lines.append("headers {")
            for h_name, h_val in headers:
                lines.append(f"  {h_name}: {h_val}")
            lines.append("}\n")

        # body:json block
        if sample_body_json:
            lines.append("body:json {")
            for b_line in sample_body_json.splitlines():
                lines.append(f"  {b_line}")
            lines.append("}\n")

        # assert block
        if self.config.include_assertions:
            lines.append("assert {")
            lines.append(f"  res.status: eq {expected_status}")
            if path == "/api/v1/system/health":
                lines.append("  res.body.status: eq ok")
            lines.append("}\n")

        return "\n".join(lines).strip() + "\n"

    def generate_collection_files(self) -> dict[str, str]:
        """Generate all collection files mapped to relative file paths."""
        files: dict[str, str] = {}

        # 1. Manifest
        files["bruno.json"] = json.dumps(self.build_bruno_json(), indent=2)

        # 2. Environment
        env_name = self.config.environment_name
        files[f"environments/{env_name}.bru"] = self.build_environment_bru()

        # 3. Request files grouped by tag
        tag_sequences: dict[str, int] = defaultdict(lambda: 1)
        paths = self.spec.get("paths", {})

        for path, methods in sorted(paths.items()):
            for method, operation in methods.items():
                if method.lower() not in [
                    "get",
                    "post",
                    "put",
                    "delete",
                    "patch",
                    "options",
                    "head",
                ]:
                    continue
                if not isinstance(operation, dict):
                    continue

                tags = operation.get("tags", ["general"])
                tag = tags[0] if tags else "general"
                folder_name = slugify(tag)

                seq = tag_sequences[folder_name]
                tag_sequences[folder_name] += 1

                op_id = (
                    operation.get("operationId") or operation.get("summary") or f"{method}_{path}"
                )
                filename = f"{seq:02d}-{slugify(op_id)}.bru"
                rel_path = f"{folder_name}/{filename}"

                bru_content = self.build_bru_file(
                    method=method,
                    path=path,
                    operation=operation,
                    sequence=seq,
                )
                files[rel_path] = bru_content

        return files

    def generate_zip_archive(self) -> bytes:
        """Create a complete ZIP archive in memory containing all Bruno collection files."""
        files = self.generate_collection_files()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for file_path, content in files.items():
                zf.writestr(file_path, content.encode("utf-8"))
        return buf.getvalue()

    def generate_json_export(self) -> dict[str, Any]:
        """Produce an inspectable JSON dictionary of the full Bruno collection."""
        files = self.generate_collection_files()
        return {
            "name": self.config.collection_name,
            "version": self.config.version,
            "environment": {
                "name": self.config.environment_name,
                "base_url": self.config.base_url,
            },
            "files_count": len(files),
            "files": files,
        }


def get_bruno_zip(app: FastAPI, config: Optional[BrunoConfig] = None) -> bytes:
    """Convenience helper to extract OpenAPI and return raw zip archive bytes."""
    generator = BrunoCollectionGenerator(app.openapi(), config=config)
    return generator.generate_zip_archive()


def get_bruno_json(app: FastAPI, config: Optional[BrunoConfig] = None) -> dict[str, Any]:
    """Convenience helper to extract OpenAPI and return structured JSON representation."""
    generator = BrunoCollectionGenerator(app.openapi(), config=config)
    return generator.generate_json_export()
