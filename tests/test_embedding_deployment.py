from pathlib import Path

import yaml


def test_local_embedding_configuration():
    compose_path = Path('deploy/embedding-compose.yaml')
    assert compose_path.exists(), 'local embedding compose is missing'
    services = yaml.safe_load(compose_path.read_text())['services']
    server = services['embedding']
    assert server['ports'] == ['127.0.0.1:17997:7997']
    assert server['mem_limit'] == '4g'
    assert server['restart'] == 'unless-stopped'
    assert len(server['command']) == 1, 'shell must receive one complete command argument'
    assert '--batch-size 2' in server['command'][0]
    assert 'python /integrity/model.py &&' in server['command'][0]
    assert server['environment']['HF_HUB_OFFLINE'] == '1'
    assert server['environment']['TRANSFORMERS_OFFLINE'] == '1'
    assert server['environment']['DO_NOT_TRACK'] == '1'
    assert any('${EMBEDDING_MODEL_DIR:-' in volume and volume.endswith(':/model/bge-m3:ro') for volume in server['volumes'])
    generator = yaml.safe_load(Path('deploy/k8s/kustomization.yaml').read_text())['configMapGenerator'][0]
    script = generator['files'][0].split('=', 1)[-1]
    assert any(script in volume for volume in server['volumes'])
    assert any(script in volume for volume in services['model-download']['volumes'])
