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
parser.add_argument('--embedding-url')
parser.add_argument('--embedding-model')
parser.add_argument('--embedding-dimension', type=int)
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
    (args.embedding_url or (
        f'http://{args.host}:31603' if args.environment == 'dev'
        else f'http://{args.host}:31613'
    )).rstrip('/') + '/embeddings',
    data=json.dumps({
        'model': args.embedding_model or (
            'model/bge-m3' if args.environment == 'dev'
            else 'model/bge-small-zh-v1.5'
        ),
        'input': ['这是一句中文。'],
    }).encode(),
    headers={'Content-Type': 'application/json'}, method='POST')
with urllib.request.urlopen(request, timeout=180) as response:
    embedding = json.load(response)['data'][0]['embedding']
dimension = args.embedding_dimension or (1024 if args.environment == 'dev' else 512)
if len(embedding) != dimension:
    raise RuntimeError(f'expected {dimension} dimensions, got {len(embedding)}')
print(f'Embedding: Chinese input returned {dimension} dimensions')
