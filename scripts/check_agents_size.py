from pathlib import Path
import sys


if __name__ == "__main__":
    path = Path("AGENTS.md")
    size = path.stat().st_size
    if size > 16000:
        print(f"{path} is {size} bytes; maximum is 16000", file=sys.stderr)
        sys.exit(1)
