"""NØMAÐ Reference — Built-in documentation and code navigation.

Provides rich, structured reference content for all NØMAÐ commands,
modules, configuration options, and concepts. Goes beyond --help
with examples, source file locations, mathematical foundations,
and cross-references.

Usage:
    nomad ref                     # list all topics
    nomad ref alerts              # alert system overview
    nomad ref dyn diversity       # dynamics diversity command
    nomad ref search "divergence" # search all documentation
"""

from nomad.reference.knowledge_base import KnowledgeBase, ReferenceEntry
from nomad.reference.formatter import ReferenceFormatter
from nomad.reference.search import SearchEngine

__all__ = ["KnowledgeBase", "ReferenceEntry", "ReferenceFormatter", "SearchEngine"]
