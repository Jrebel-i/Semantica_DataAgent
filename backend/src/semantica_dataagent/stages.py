"""Ordered registration of independent knowledge-writing stages."""

from .table_stage import write as write_tables

STAGES = [write_tables]
