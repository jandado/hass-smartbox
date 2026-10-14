"""REST addressing primitives.

``NodeRef``, segment encoding and node paths are pure addressing, shared by the
client (which builds read paths) and the named write operations (which build
write paths), so neither module has to import the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

__all__ = ["NodeRef", "encode_segment", "node_path"]


@dataclass(frozen=True)
class NodeRef:
    """Path reference to one node: ``devs/<dev>/<node_type>/<addr>/...``."""

    node_type: str
    addr: str


def encode_segment(value: str) -> str:
    """URL-encode one path segment (dev ids/node addresses are opaque)."""
    return quote(value, safe="")


def node_path(dev_id: str, node: NodeRef, leaf: str) -> str:
    """Build ``devs/<dev>/<type>/<addr>/<leaf>``."""
    return f"devs/{encode_segment(dev_id)}/{encode_segment(node.node_type)}/{encode_segment(node.addr)}/{leaf}"
