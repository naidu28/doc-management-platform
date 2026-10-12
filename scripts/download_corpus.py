"""Download public fixture sources only; no application data or credentials."""
import urllib.request
from pathlib import Path

RAW = Path(__file__).resolve().parents[1] / 'data/raw'
SOURCES = {
    'cuad.json': 'https://huggingface.co/datasets/theatticusproject/cuad/resolve/main/CUAD_v1/CUAD_v1.json',
    'CUAD_LICENSE_METADATA.md': 'https://huggingface.co/datasets/theatticusproject/cuad/raw/main/README.md',
    'uscode42.zip': 'https://uscode.house.gov/download/releasepoints/us/pl/119/120/xml_usc42@119-120.zip',
}


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    for name, url in SOURCES.items():
        path = RAW / name
        if not path.exists():
            with urllib.request.urlopen(url, timeout=120) as response:
                payload = response.read()
            temporary = path.with_suffix(path.suffix + '.partial')
            temporary.write_bytes(payload)
            temporary.replace(path)
        print(name, path.stat().st_size)
    (RAW/'uscode42.source.txt').write_text(SOURCES['uscode42.zip'])


if __name__ == '__main__':
    main()
