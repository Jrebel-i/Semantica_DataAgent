"""Ordered registration of independent knowledge-writing stages."""

from .dimension_stage import write as write_dimensions
from .document_stage import write as write_documents
from .table_stage import write as write_tables
from .task_stage import write as write_tasks

STAGES = [write_tables, write_dimensions, write_documents, write_tasks]
