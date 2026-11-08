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


def _k8s_documents():
    documents = yaml.safe_load_all(Path('deploy/k8s/embedding.yaml').read_text())
    return {(document['kind'], document['metadata']['name']): document for document in documents if document}


def test_k8s_embedding_is_a_four_replica_statefulset():
    resources = _k8s_documents()
    assert not any(kind == 'Deployment' for kind, _ in resources), 'old Deployment definition must be removed'
    assert not any(
        kind == 'PersistentVolumeClaim' for kind, _ in resources
    ), 'the model volume is now owned by volumeClaimTemplates, not a standalone PVC'

    service = resources[('Service', 'embedding')]
    assert service['spec']['type'] == 'NodePort'
    assert service['spec']['selector'] == {'app': 'embedding'}
    ports = {port['port']: port for port in service['spec']['ports']}
    assert ports[7997]['targetPort'] == 7997
    assert ports[7997]['nodePort'] == 31603

    statefulset = resources[('StatefulSet', 'embedding-pool')]
    spec = statefulset['spec']
    assert spec['replicas'] == 4
    assert spec['podManagementPolicy'] == 'Parallel'
    assert spec['serviceName'] == 'embedding'
    assert spec['selector']['matchLabels'] == {'app': 'embedding'}
    pod = spec['template']
    assert pod['metadata']['labels'] == {'app': 'embedding'}

    pod_spec = pod['spec']
    init_containers = {container['name']: container for container in pod_spec['initContainers']}
    containers = {container['name']: container for container in pod_spec['containers']}
    init = init_containers['download-and-verify']
    assert init['command'] == ['python', '/integrity/model.py', 'download']

    infinity = containers['infinity']
    command = ' '.join(infinity['command'] + infinity['args'])
    assert '--batch-size 16' in command
    assert '--port 7997' in command
    assert '--host 0.0.0.0' in command

    requests = infinity['resources']['requests']
    assert requests['cpu'] == 2
    assert requests['memory'] == '2Gi'
    assert infinity['resources']['limits'] == {'memory': '4Gi'}
    assert infinity['readinessProbe']['httpGet'] == {'path': '/health', 'port': 7997}
    env = {item['name']: item.get('value') for item in infinity['env']}
    assert env == {'DO_NOT_TRACK': '1', 'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1'}

    claims = spec['volumeClaimTemplates']
    assert len(claims) == 1
    claim = claims[0]
    assert claim['metadata']['name'] == 'model'
    assert claim['spec']['storageClassName'] == 'local-path'
    assert claim['spec']['accessModes'] == ['ReadWriteOnce']
    assert claim['spec']['resources']['requests']['storage'] == '10Gi'

    init_mounts = {mount['name']: mount for mount in init['volumeMounts']}
    assert init_mounts['model']['mountPath'] == '/model'
    assert 'readOnly' not in init_mounts['model']
    assert init_mounts['integrity']['mountPath'] == '/integrity'
    infinity_mounts = {mount['name']: mount for mount in infinity['volumeMounts']}
    assert infinity_mounts['model']['mountPath'] == '/model'
    assert infinity_mounts['model']['readOnly'] is True
    assert infinity_mounts['integrity']['mountPath'] == '/integrity'
    assert {mount['name'] for mount in init['volumeMounts']} == {'model', 'integrity'}
    assert {mount['name'] for mount in infinity['volumeMounts']} == {'model', 'integrity'}

    volumes = {volume['name']: volume for volume in pod_spec['volumes']}
    assert volumes['integrity']['configMap']['name'] == 'embedding-integrity'
    assert set(volumes) == {'integrity'}

    anti_affinity = pod_spec['affinity']['podAntiAffinity']
    terms = anti_affinity['preferredDuringSchedulingIgnoredDuringExecution']
    assert terms
    assert any(
        term['podAffinityTerm']['topologyKey'] == 'kubernetes.io/hostname'
        and term['podAffinityTerm']['labelSelector']['matchLabels'] == {'app': 'embedding'}
        for term in terms
    )
