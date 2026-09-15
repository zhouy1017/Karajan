"""Read the declared YAML subset without tags, duplicates, aliases or depth.

The parser is the repository's declared safe entry point plus four additions,
each closing a way a document could mean something other than it says:

* A duplicate mapping key is rejected instead of resolving last-write-wins.
* An alias, anchor or merge key is rejected outright, so a document cannot
  expand into a structure whose size is not visible in its own text and a
  self-referential alias cannot produce an infinite tree.
* Nesting depth, collection size and scalar length are bounded, so a small
  upload cannot become an unbounded in-memory structure.
* Scalars that name an evaluation are refused, because a declarative condition
  uses the typed operators in ``compiler.py`` and never free text.

No custom tag, ``!!python`` object, import or executable expression is
constructible: the loader is the safe one, its constructor table is not
extended, and the node composer refuses reference events before they expand.
"""

from typing import Any

import yaml  # type: ignore[import-untyped]
from yaml.events import AliasEvent, ScalarEvent  # type: ignore[import-untyped]

from .errors import WorkflowError, located

MAXIMUM_DEPTH = 24
#: Transport-safety bounds on the upload itself. There is deliberately no limit
#: on how many steps, tasks, roles or expansion members a legal document may
#: declare: a task count is a scheduling concern, never a parser policy, and the
#: product imposes no engine-level agent or task cap. The only ceilings here bound
#: the work one parse may do, and they are expressed in nodes and bytes rather
#: than in business objects, so a large but ordinary configuration is accepted.
#:
#: ``MAXIMUM_DOCUMENT_BYTES`` is the real bound: a 2 MiB document cannot contain
#: more than roughly 20k short steps, which is already far beyond any legal
#: configuration this slice must carry. ``MAXIMUM_NODES`` is a generous multiple
#: of that, so it never becomes the effective cap on tasks.
MAXIMUM_NODES = 200_000
MAXIMUM_SCALAR_LENGTH = 8_192
MAXIMUM_DOCUMENT_BYTES = 2_097_152

#: Words that name an executable expression rather than a declarative value.
EXPRESSION_PREFIXES = ("${", "#{", "!{", "{{", "{{", "<%", "=(", "eval(", "exec(")


class _BundleLoader(yaml.SafeLoader):  # type: ignore[misc]
    """SafeLoader that refuses references, merge keys and duplicate keys."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        """Refuse an anchor or alias before it expands into shared nodes.

        PyYAML exposes no ``AnchorEvent``: an anchor is an ``anchor`` attribute on
        the node's own event, while an alias is a distinct event. Both are
        refused here, in the composer, before any node is constructed, so a
        self-referential alias can never be expanded even once.
        """
        if self.check_event(AliasEvent):
            raise WorkflowError("WORKFLOW_YAML_ALIAS_UNSUPPORTED")
        event = self.peek_event()
        if getattr(event, "anchor", None) is not None:
            raise WorkflowError("WORKFLOW_YAML_ALIAS_UNSUPPORTED")
        if isinstance(event, ScalarEvent) and event.value in {"<<", "="}:
            # ``<<`` would merge another mapping in; ``=`` is the default-value
            # key, which is not part of a declarative configuration.
            raise WorkflowError("WORKFLOW_YAML_UNSAFE")
        return super().compose_node(parent, index)

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, yaml.MappingNode):
            raise WorkflowError("WORKFLOW_YAML_UNSAFE")
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, (str, int, float, bool)) and key is not None:
                raise WorkflowError("WORKFLOW_YAML_UNSAFE")
            if key in mapping:
                raise WorkflowError(
                    "WORKFLOW_YAML_DUPLICATE_KEY",
                    diagnostics=[
                        located(
                            "WORKFLOW_YAML_DUPLICATE_KEY",
                            f"workflow.yaml#/line-{key_node.start_mark.line + 1}",
                            "duplicate mapping key",
                        )
                    ],
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _refuse(node: yaml.Node) -> Any:
    del node
    raise WorkflowError("WORKFLOW_YAML_UNSAFE")


_BundleLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    lambda loader, node: loader.construct_mapping(node),
)
_BundleLoader.add_constructor("tag:yaml.org,2002:merge", _refuse)
_BundleLoader.add_constructor("tag:yaml.org,2002:python/object", _refuse)
_BundleLoader.add_constructor("tag:yaml.org,2002:python/name", _refuse)
_BundleLoader.add_constructor("tag:yaml.org,2002:timestamp", _refuse)
_BundleLoader.add_constructor("!python/object", _refuse)


def load(text: str, *, file: str = "workflow.yaml") -> Any:
    """Parse one declared YAML document into plain Python containers."""
    if len(text.encode("utf-8", errors="ignore")) > MAXIMUM_DOCUMENT_BYTES:
        raise WorkflowError("WORKFLOW_CONTENT_TOO_LARGE")
    try:
        document = yaml.load(text, Loader=_BundleLoader)  # noqa: S506 - bounded SafeLoader
    except WorkflowError:
        raise
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        pointer = (
            f"#/line-{mark.line + 1}-column-{mark.column + 1}" if mark is not None else "#/"
        )
        raise WorkflowError(
            "WORKFLOW_YAML_INVALID",
            diagnostics=[
                located("WORKFLOW_YAML_INVALID", f"{file}{pointer}", "YAML parse failed")
            ],
        ) from None
    except RecursionError:
        raise WorkflowError("WORKFLOW_YAML_TOO_DEEP") from None
    except (ValueError, TypeError):
        raise WorkflowError("WORKFLOW_YAML_UNSAFE") from None
    _bounded(document, depth=0)
    _refuse_expressions(document, file=file, pointer="#/")
    return document

def _bounded(value: Any, *, depth: int) -> None:
    """Reject a structure deeper or larger than the transport-safety limits.

    The limits are counted in nodes, not in steps or tasks. A workflow with
    thousands of steps is an ordinary document; it is the total size of the
    parsed structure that is bounded, so no business-level count is smuggled in
    as a parser policy.
    """
    if depth > MAXIMUM_DEPTH:
        raise WorkflowError(
            "WORKFLOW_YAML_TOO_DEEP",
            diagnostics=[
                located("WORKFLOW_YAML_TOO_DEEP", "workflow.yaml", "nesting exceeds limit")
            ],
        )
    counter = _Counter()
    if not _within_node_budget(value, counter):
        raise WorkflowError("WORKFLOW_CONTENT_TOO_LARGE")


class _Counter:
    __slots__ = ("nodes",)

    def __init__(self) -> None:
        self.nodes = 0


def _within_node_budget(value: Any, counter: _Counter) -> bool:
    """Walk the whole structure iteratively, counting nodes and depth.

    Iterative rather than recursive: a deeply nested document must be refused by
    the declared depth limit, not by a Python recursion error, and a very wide
    document must not exhaust the interpreter stack either.
    """
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        counter.nodes += 1
        if counter.nodes > MAXIMUM_NODES:
            return False
        if depth > MAXIMUM_DEPTH:
            raise WorkflowError(
                "WORKFLOW_YAML_TOO_DEEP",
                diagnostics=[
                    located("WORKFLOW_YAML_TOO_DEEP", "workflow.yaml", "nesting exceeds limit")
                ],
            )
        if isinstance(item, dict):
            for key, nested in item.items():
                stack.append((key, depth + 1))
                stack.append((nested, depth + 1))
        elif isinstance(item, list):
            for nested in item:
                stack.append((nested, depth + 1))
        elif isinstance(item, str) and len(item) > MAXIMUM_SCALAR_LENGTH:
            raise WorkflowError("WORKFLOW_CONTENT_TOO_LARGE")
    return True


def dump(document: Any) -> str:
    """Serialise a structural edit deterministically and safely.

    The same document always produces the same text, so a table edit is
    reproducible: two edits that produce equal structures produce equal bytes.
    Keys are sorted, block style is used and the output is re-parsed here, so an
    edit can never write a document this module would refuse to read back.
    """
    try:
        text = yaml.safe_dump(
            document,
            sort_keys=True,
            default_flow_style=False,
            allow_unicode=True,
            width=4096,
            line_break="\n",
        )
    except (yaml.YAMLError, ValueError, RecursionError):
        raise WorkflowError("WORKFLOW_YAML_UNSAFE") from None
    dumped: str = text
    # A dumped document is re-read through the same strict parser before it is
    # stored, so an edit cannot smuggle in something the reader rejects.
    load(dumped, file="workflow.yaml")
    return dumped.rstrip("\n")


def _refuse_expressions(value: Any, *, file: str, pointer: str) -> None:
    """Refuse an executable-looking scalar anywhere in the document.

    Iterative for the same reason the node budget is: a wide document must be
    judged by the declared limits, not by the interpreter's recursion limit.
    """
    stack: list[tuple[Any, str]] = [(value, pointer)]
    while stack:
        item, here = stack.pop()
        if isinstance(item, dict):
            for key, nested in item.items():
                nested_pointer = f"{here}{str(key)[:32]}/"
                stack.append((key, nested_pointer))
                stack.append((nested, nested_pointer))
        elif isinstance(item, list):
            for index, nested in enumerate(item):
                stack.append((nested, f"{here}{index}/"))
        elif isinstance(item, str):
            stripped = item.lstrip()
            for prefix in EXPRESSION_PREFIXES:
                if stripped.startswith(prefix):
                    raise WorkflowError(
                        "WORKFLOW_EXPRESSION_UNSUPPORTED",
                        diagnostics=[
                            located(
                                "WORKFLOW_EXPRESSION_UNSUPPORTED",
                                f"{file}{here.rstrip('/') or '#/'}",
                                "expressions are not part of the declarative schema",
                            )
                        ],
                    )
