"""Download and verify one pinned embedding model before it is served."""

import argparse
import hashlib
import os
import pathlib
import urllib.parse
import urllib.request


MODELS = {
    "bge-m3": {
        "repo": "BAAI/bge-m3",
        "revision": "5617a9f61b028005a4858fdac845db406aefb181",
        "root": pathlib.Path("/model/bge-m3"),
        "files": {
            "1_Pooling/config.json": "e54c164a07274f2eb45bb724f54a79d1efcc90c41573887cd9a29aeee0597352",
            "config.json": "26159e7ad065073448460117eb24b7a4572f6f4e78eadff65dc0a11c052449fa",
            "config_sentence_transformers.json": "1eef72430e7194a1e59680e635aed81ffa083f05668dbc5bb1c56c04c0999c38",
            "modules.json": "84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf",
            "pytorch_model.bin": "b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38",
            "sentence_bert_config.json": "eb9b44b13c0f52a3b3685c3b1cbdea1ba8b04bea123b98f61610048940776eb1",
            "sentencepiece.bpe.model": "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
            "special_tokens_map.json": "8c785abebea9ae3257b61681b4e6fd8365ceafde980c21970d001e834cf10835",
            "tokenizer.json": "21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08",
            "tokenizer_config.json": "a62b2b6784f990259fddef5f16388693a8043be4f69179e6a5257eeb3f9abac4",
        },
    },
    "bge-small-zh-v1.5": {
        "repo": "BAAI/bge-small-zh-v1.5",
        "revision": "7999e1d3359715c523056ef9478215996d62a620",
        "root": pathlib.Path("/model/bge-small-zh-v1.5"),
        "files": {
            "1_Pooling/config.json": "aaa8861589f80c961a03cc86c7eeaef7605c1676b9ab55329d33a304738769c6",
            "config.json": "3853a7979202c348751b753e36f579c41d8da7d36af617d3d907e1fc9b441f2a",
            "config_sentence_transformers.json": "940d5f50db195fa6e5e6a4f122c095f77880de259d74b14a65779ed48bdd7c56",
            "modules.json": "84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf",
            "pytorch_model.bin": "7c5fe667bbed05dc10e246e229b701ad266fe4d95ab946e9e5aa402056611b88",
            "sentence_bert_config.json": "84e39fda68ccbff05bfa723ae9c0e70e23e2ec373b76e0f8c6e71af72a693cbf",
            "special_tokens_map.json": "b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3",
            "tokenizer.json": "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
            "tokenizer_config.json": "e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a",
            "vocab.txt": "45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c",
        },
    },
}


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1048576), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify(root, files, *, download=False, repo=None, revision=None):
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for name, expected in files.items():
        path = root / name
        if download:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or digest(path) != expected:
                endpoint = os.environ.get("HF_ENDPOINT", "https://huggingface.co")
                url = (
                    endpoint.rstrip("/")
                    + "/"
                    + repo
                    + "/resolve/"
                    + revision
                    + "/"
                    + urllib.parse.quote(name, safe="/")
                )
                tmp = path.with_name(path.name + ".partial")
                with urllib.request.urlopen(url, timeout=180) as response, tmp.open(
                    "wb"
                ) as output:
                    while chunk := response.read(1048576):
                        output.write(chunk)
                tmp.replace(path)
        actual = digest(path)
        if actual != expected:
            raise SystemExit("SHA256 mismatch: " + name)
        print("verified " + name, flush=True)
    extra = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file()}
    extra -= set(files)
    if extra:
        raise SystemExit("Unexpected model files: " + ", ".join(sorted(extra)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", nargs="?", choices=("verify", "download"), default="verify"
    )
    parser.add_argument("--model", choices=sorted(MODELS), default="bge-m3")
    args = parser.parse_args(argv)
    spec = MODELS[args.model]
    verify(
        spec["root"],
        spec["files"],
        download=args.action == "download",
        repo=spec["repo"],
        revision=spec["revision"],
    )


if __name__ == "__main__":
    main()
