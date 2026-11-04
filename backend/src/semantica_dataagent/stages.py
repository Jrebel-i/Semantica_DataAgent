"""Ordered registration of independent knowledge-writing stages."""

from .dimension_stage import write as write_dimensions
from .document_stage import write as write_documents
from .table_stage import write as write_tables

STAGES = [write_tables, write_dimensions, write_documents]
