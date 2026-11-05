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


def test_daily_test_embedding_is_a_separate_small_model():
    services = yaml.safe_load(Path('deploy/embedding-compose.yaml').read_text())['services']
    assert {'embedding', 'embedding-small'} <= set(services)
    server = services['embedding-small']
    download = services['model-download-small']
    assert server['ports'] == ['127.0.0.1:17998:7997']
    assert server['mem_limit'] == '2g'
    assert server['restart'] == 'unless-stopped'
    assert len(server['command']) == 1
    command = server['command'][0]
    assert '--model bge-small-zh-v1.5' in command
    assert '--model-id /model/bge-small-zh-v1.5' in command
    assert '--batch-size 8' in command
    assert 'python /integrity/model.py' in command
    assert server['environment']['HF_HUB_OFFLINE'] == '1'
    assert server['environment']['TRANSFORMERS_OFFLINE'] == '1'
    assert server['environment']['DO_NOT_TRACK'] == '1'
    assert any(
        '${EMBEDDING_SMALL_MODEL_DIR:-' in volume
        and volume.endswith(':/model/bge-small-zh-v1.5:ro')
        for volume in server['volumes']
    )
    assert download['command'][-2:] == ['--model', 'bge-small-zh-v1.5']
    assert any(
        '${EMBEDDING_SMALL_MODEL_DIR:-' in volume
        and volume.endswith(':/model/bge-small-zh-v1.5')
        for volume in download['volumes']
    )
    assert not any('bge-m3' in volume for volume in server['volumes'])
    assert not any('bge-small' in volume for volume in services['embedding']['volumes'])
