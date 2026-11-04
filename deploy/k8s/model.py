import hashlib, os, pathlib, sys, urllib.request
root = pathlib.Path('/model/bge-m3')
files = {'1_Pooling/config.json': 'e54c164a07274f2eb45bb724f54a79d1efcc90c41573887cd9a29aeee0597352', 'config.json': '26159e7ad065073448460117eb24b7a4572f6f4e78eadff65dc0a11c052449fa', 'config_sentence_transformers.json': '1eef72430e7194a1e59680e635aed81ffa083f05668dbc5bb1c56c04c0999c38', 'modules.json': '84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf', 'pytorch_model.bin': 'b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38', 'sentence_bert_config.json': 'eb9b44b13c0f52a3b3685c3b1cbdea1ba8b04bea123b98f61610048940776eb1', 'sentencepiece.bpe.model': 'cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865', 'special_tokens_map.json': '8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835', 'tokenizer.json': '21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08', 'tokenizer_config.json': 'a62b2b6784f990259fddef5f16388693a8043be4f69179e6a5257eeb3f9abac4'}
revision = '5617a9f61b028005a4858fdac845db406aefb181'
def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1048576), b''):
            h.update(chunk)
    return h.hexdigest()
root.mkdir(parents=True, exist_ok=True)
for name, expected in files.items():
    path = root / name
    if len(sys.argv) > 1 and sys.argv[1] == 'download':
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or digest(path) != expected:
            url = os.environ.get('HF_ENDPOINT', 'https://huggingface.co').rstrip('/') + '/BAAI/bge-m3/resolve/' + revision + '/' + name
            tmp = path.with_name(path.name + '.partial')
            with urllib.request.urlopen(url, timeout=180) as response, tmp.open('wb') as out:
                while chunk := response.read(1048576):
                    out.write(chunk)
            tmp.replace(path)
    actual = digest(path)
    if actual != expected:
        raise SystemExit('SHA256 mismatch: ' + name)
    print('verified ' + name, flush=True)
extra = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()} - set(files)
if extra:
    raise SystemExit('Unexpected model files: ' + ', '.join(sorted(extra)))
