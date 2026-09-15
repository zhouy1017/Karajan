"""Declarative workflow bundles: real files, a pure compiler and a derived preview.

This package stores actual bundle bytes as immutable revisions, compiles them
with a pure declarative compiler, and projects the graph, table, diagram and diff
from one compiled result. It does not activate a configuration, start a Run,
call a model or execute a business adapter at compile time. See
[可定制角色与 Workflow](../../../docs/architecture/09-configurable-workflows.md),
[对话式 Workflow](../../../docs/architecture/10-conversational-workflow-deployment.md)
and the implementation record [R8-P1-02](../../../docs/implementation/r8-phase1-workflows.md).
"""

from .bundle import (
    Bundle,
    BundleFile,
    compile_bundle,
    manifest_document,
    read_directory,
    validate_manifest,
)
from .compiler import (
    COMPILER_IDENTITY,
    COMPILER_REVISION,
    CompiledStep,
    CompiledWorkflow,
    compile_workflow,
    diff,
    escape_label,
    projection,
)
from .errors import Diagnostic, WorkflowError
from .layout import BundlePaths
from .registry import (
    DECLARED_KINDS,
    ExecutionKind,
    InputContract,
    OutputContract,
    artifact_aggregate,
    kind_for,
    registered_refs,
)
from .store import WorkflowStore

__all__ = [
    "Bundle",
    "BundleFile",
    "BundlePaths",
    "COMPILER_IDENTITY",
    "COMPILER_REVISION",
    "CompiledStep",
    "CompiledWorkflow",
    "DECLARED_KINDS",
    "Diagnostic",
    "ExecutionKind",
    "InputContract",
    "OutputContract",
    "WorkflowError",
    "WorkflowStore",
    "artifact_aggregate",
    "compile_bundle",
    "compile_workflow",
    "diff",
    "escape_label",
    "kind_for",
    "manifest_document",
    "projection",
    "read_directory",
    "registered_refs",
    "validate_manifest",
]
