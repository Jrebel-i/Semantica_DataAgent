import yaml
from pathlib import Path
import subprocess


def test_test_embedding_is_isolated():
    overlay = Path('deploy/embedding-test')
    assert overlay.exists(), 'test embedding deployment is missing'
    rendered = subprocess.check_output(['kubectl', 'kustomize', str(overlay)], text=True)
    objects = {item['kind']: item for item in yaml.safe_load_all(rendered)}
    assert all(obj['metadata']['namespace'] == 'semantica-dataagent-test' for obj in objects.values())
    assert objects['Service']['spec']['ports'][0]['nodePort'] == 31613
    base = list(yaml.safe_load_all(Path('deploy/k8s/embedding.yaml').read_text()))
    assert objects['ConfigMap']['data']['model.py'] == base[0]['data']['model.py']
    pod = objects['Deployment']['spec']['template']['spec']
    preferences = pod['affinity']['nodeAffinity']['preferredDuringSchedulingIgnoredDuringExecution']
    assert any('k8s-worker-02' in term['preference']['matchExpressions'][0]['values'] for term in preferences)
    assert 'tolerations' not in pod
