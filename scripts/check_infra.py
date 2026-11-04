#!/usr/bin/env python3
"""Exercise the isolated services through their NodePorts."""
import argparse
import json
import os
import urllib.request
import uuid

from neo4j import GraphDatabase
from pymilvus import MilvusClient

parser = argparse.ArgumentParser()
parser.add_argument('environment', choices=['dev', 'test'])
parser.add_argument('--host', default='172.16.242.124')
parser.add_argument('--skip-embedding', action='store_true', help='Check databases while embedding is unavailable')
args = parser.parse_args()
password = os.environ['NEO4J_PASSWORD']
neo_port, milvus_port = (31601, 31602) if args.environment == 'dev' else (31611, 31612)
with GraphDatabase.driver(f'bolt://{args.host}:{neo_port}', auth=('neo4j', password)) as driver:
    records, _, _ = driver.execute_query('RETURN 1 AS value')
    if records[0]['value'] != 1:
        raise RuntimeError('Neo4j returned an unexpected value')
    print('Neo4j: Cypher returned 1')
client = MilvusClient(uri=f'http://{args.host}:{milvus_port}')
name = 'check_' + uuid.uuid4().hex
try:
    client.create_collection(collection_name=name, dimension=8)
    if not client.has_collection(name):
        raise RuntimeError('Milvus collection was not created')
finally:
    if client.has_collection(name):
        client.drop_collection(name)
    client.close()
print('Milvus: collection created and deleted')
if args.skip_embedding:
    raise SystemExit(0)
request = urllib.request.Request(
    f'http://{args.host}:{31603 if args.environment == "dev" else 31613}/embeddings',
    data=json.dumps({'model': 'model/bge-m3', 'input': ['这是一句中文。']}).encode(),
    headers={'Content-Type': 'application/json'}, method='POST')
with urllib.request.urlopen(request, timeout=180) as response:
    embedding = json.load(response)['data'][0]['embedding']
if len(embedding) != 1024:
    raise RuntimeError(f'expected 1024 dimensions, got {len(embedding)}')
print('Embedding: Chinese input returned 1024 dimensions')
