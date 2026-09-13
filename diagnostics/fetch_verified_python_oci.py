"""Fetch the pinned official Python image for an offline linux/amd64 Docker build.

No account credentials are used. Every index, manifest, config and layer is
verified against its SHA-256 before being placed in the OCI archive.
"""
import hashlib
import argparse
import json
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

INDEX = "sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2"
AMD64 = "sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af"
TAG = "docker.io/library/python:3.12.13-slim-bookworm"
ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "output/platform-audit-implementation/python-3.12.13-amd64-attested.oci.tar"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging", type=Path)
    args = parser.parse_args()
    if TARGET.exists():
        raise SystemExit("Archive already exists; inspect and reuse it rather than overwrite.")
    temporary = args.staging or Path(tempfile.mkdtemp(prefix="start-stop-python-oci-", dir="/private/tmp"))
    if temporary.is_symlink() or temporary.resolve().parent != Path("/private/tmp") or not temporary.name.startswith("start-stop-python-oci-"):
        raise ValueError("Use only this task's image staging directory")
    print(f"STAGING={temporary}", flush=True)
    if shutil.disk_usage(temporary).free < 512 * 1024**2:
        raise SystemExit("Less than 512 MiB available for the verified image archive.")
    opener = urllib.request.build_opener()
    auth_url = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull"
    with opener.open(auth_url, timeout=20) as response:
        token = json.load(response)["token"]
    headers = {"Authorization": "Bearer " + token, "Accept": ",".join([
        "application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.docker.distribution.manifest.v2+json"])}
    blobs = temporary / "blobs/sha256"
    blobs.mkdir(parents=True, exist_ok=True)
    fetched = set()

    def fetch(digest, *, manifest=False, size=None):
        if not digest.startswith("sha256:") or len(digest) != 71 or any(c not in "0123456789abcdef" for c in digest[7:]):
            raise ValueError("Invalid digest")
        destination = blobs / digest[7:]
        if digest in fetched:
            return destination
        if destination.is_file() and not destination.is_symlink():
            checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
            if checksum != digest[7:] or (size is not None and destination.stat().st_size != size):
                raise ValueError("Existing staged component failed verification")
            fetched.add(digest)
            return destination
        limit = 2*1024**2 if manifest else 128*1024**2
        if size is not None and size > limit:
            raise ValueError("Unexpected image component size")
        endpoint = "manifests" if manifest else "blobs"
        request = urllib.request.Request(f"https://registry-1.docker.io/v2/library/python/{endpoint}/{digest}", headers=headers)
        count, checksum = 0, hashlib.sha256()
        with opener.open(request, timeout=30) as response, destination.open("xb") as target:
            while chunk := response.read(1024*1024):
                count += len(chunk)
                if count > limit:
                    raise ValueError("Image component exceeded its bound")
                checksum.update(chunk)
                target.write(chunk)
        if checksum.hexdigest() != digest[7:] or (size is not None and count != size):
            raise ValueError("Image component SHA-256 or byte count mismatch")
        fetched.add(digest)
        print(f"VERIFIED {digest} {count}", flush=True)
        return destination

    index_path = fetch(INDEX, manifest=True)
    index = json.loads(index_path.read_bytes())
    matches = [item for item in index["manifests"] if item.get("platform", {}).get("os") == "linux" and item.get("platform", {}).get("architecture") == "amd64"]
    if len(matches) != 1 or matches[0]["digest"] != AMD64:
        raise ValueError("Pinned platform descriptor changed")
    descriptor = matches[0]
    manifest = json.loads(fetch(AMD64, manifest=True, size=descriptor["size"]).read_bytes())
    for item in [manifest["config"], *manifest["layers"]]:
        fetch(item["digest"], size=item["size"])
    for entry in index["manifests"]:
        if entry.get("annotations", {}).get("vnd.docker.reference.digest") == AMD64:
            attestation = json.loads(fetch(entry["digest"], manifest=True, size=entry["size"]).read_bytes())
            for item in [attestation["config"], *attestation["layers"]]:
                fetch(item["digest"], size=item["size"])
    (temporary / "oci-layout").write_text('{"imageLayoutVersion":"1.0.0"}')
    (temporary / "index.json").write_text(json.dumps({"schemaVersion":2,"manifests":[{
        "mediaType":index["mediaType"],"digest":INDEX,"size":index_path.stat().st_size,
        "annotations":{"io.containerd.image.name":TAG,"org.opencontainers.image.ref.name":TAG},
    }]}, separators=(",", ":")))
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    with TARGET.open("xb") as output, tarfile.open(fileobj=output, mode="w") as archive:
        for path in sorted(temporary.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(temporary).as_posix(), recursive=False)
    digest = hashlib.sha256()
    with TARGET.open("rb") as archive:
        while chunk := archive.read(1024*1024):
            digest.update(chunk)
    print(json.dumps({"archive":str(TARGET),"sha256":digest.hexdigest(),"bytes":TARGET.stat().st_size,
        "official_index":INDEX,"platform_manifest":AMD64,"platform":"linux/amd64"}), flush=True)


if __name__ == "__main__":
    main()
