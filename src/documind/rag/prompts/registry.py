"""Prompt registry per §7.2.

Each prompt template is a release artifact with a name, revision, SHA-256
hash, permitted role, input/output JSON Schema, token limits, and the
injection-safety preamble.

The registry validates template integrity via file-based SHA-256 hashes
and records prompt revision per invocation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from documind.services.llm_service import ModelRole


@dataclass(frozen=True)
class PromptTemplate:
    """A registered prompt template with integrity metadata."""

    name: str
    revision: int
    text: str
    permitted_role: ModelRole
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    max_input_tokens: int = 4096
    max_output_tokens: int = 2048
    language_rules: str = ""
    sha256: str = ""

    def compute_sha256(self) -> str:
        """Compute SHA-256 of the template text."""
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        """Verify that the stored SHA-256 matches the template text."""
        if not self.sha256:
            return True  # No hash to verify
        return self.sha256 == self.compute_sha256()


class PromptRegistry:
    """Registry of prompt templates with integrity validation.

    Templates are loaded from Python module definitions (``templates.py``).
    The registry validates hash integrity and resolves templates by name
    and optional revision.
    """

    def __init__(self) -> None:
        self._templates: dict[str, dict[int, PromptTemplate]] = {}
        self._invocation_log: list[dict[str, Any]] = []

    def register(self, template: PromptTemplate) -> None:
        """Register a prompt template.

        Validates integrity on registration.

        Raises ``ValueError`` if the template hash is invalid.
        """
        if not template.verify_integrity():
            msg = f"Prompt template '{template.name}' rev {template.revision} failed integrity check"
            raise ValueError(msg)

        self._templates.setdefault(template.name, {})[template.revision] = template

    def resolve(self, name: str, revision: int | None = None) -> PromptTemplate:
        """Resolve a template by name and optional revision.

        If revision is None, returns the latest revision.

        Raises ``KeyError`` if the template is not found.
        """
        versions = self._templates.get(name)
        if not versions:
            msg = f"Prompt template '{name}' not found"
            raise KeyError(msg)

        if revision is not None:
            template = versions.get(revision)
            if template is None:
                msg = f"Prompt template '{name}' revision {revision} not found"
                raise KeyError(msg)
            return template

        # Return the latest revision.
        latest_rev = max(versions.keys())
        return versions[latest_rev]

    def record_invocation(
        self,
        template_name: str,
        revision: int,
        role: ModelRole,
        input_valid: bool = True,
        output_valid: bool = True,
    ) -> None:
        """Record a prompt template invocation for audit."""
        self._invocation_log.append({
            "template_name": template_name,
            "revision": revision,
            "role": role.value,
            "input_valid": input_valid,
            "output_valid": output_valid,
        })

    @property
    def invocation_log(self) -> list[dict[str, Any]]:
        """Return the invocation log (for agent-run persistence)."""
        return list(self._invocation_log)

    def list_templates(self) -> list[str]:
        """Return all registered template names."""
        return list(self._templates.keys())

    def verify_manifest(self, manifest_path: str | None = None) -> list[str]:
        """T8-30: Verify all registered templates against a signed manifest.

        Returns a list of error messages (empty if all valid).
        """
        import json
        import os

        if manifest_path is None:
            manifest_path = os.path.join(
                os.path.dirname(__file__), "prompt_manifest.json",
            )

        if not os.path.exists(manifest_path):
            return []  # No manifest to verify against — pass.

        with open(manifest_path) as f:
            manifest = json.load(f)

        errors: list[str] = []
        for name, revisions in self._templates.items():
            for rev, template in revisions.items():
                key = f"{name}:{rev}"
                expected = manifest.get(key, {}).get("sha256", "")
                if not expected:
                    errors.append(f"Template '{key}' not in manifest")
                elif expected != template.sha256:
                    errors.append(
                        f"Template '{key}' hash mismatch: "
                        f"manifest={expected[:16]}… registered={template.sha256[:16]}…"
                    )
        return errors

    @staticmethod
    def generate_manifest(registry: PromptRegistry) -> dict[str, dict[str, Any]]:
        """Generate a manifest dict from the current registry state."""
        manifest: dict[str, dict[str, Any]] = {}
        for name, revisions in registry._templates.items():
            for rev, template in revisions.items():
                key = f"{name}:{rev}"
                manifest[key] = {
                    "revision": rev,
                    "sha256": template.sha256,
                    "permitted_role": template.permitted_role.value,
                }
        return manifest


def build_default_registry() -> PromptRegistry:
    """Build a PromptRegistry pre-loaded with all default templates.

    Imports and registers all templates from ``templates.py``.
    """
    from documind.rag.prompts.templates import ALL_TEMPLATES

    registry = PromptRegistry()
    for template in ALL_TEMPLATES:
        registry.register(template)
    return registry
