"""Build and verify the tracked CoreGeek deployment files, without caches.

Run from any directory after staging new deployment files.
"""
import gzip
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile


def main():
    root=Path(__file__).resolve().parents[3]
    paths=subprocess.check_output(['git','-C',str(root),'ls-files','-z','Demo/CoreGeek'],text=True).split('\0')
    files=[root/p for p in paths if p and not any(part.startswith('._') or part in
           ('__pycache__','.pytest_cache','.DS_Store') for part in Path(p).parts)]
    output=root/'Demo/CoreGeek.tar.gz'
    with output.open('wb') as raw, gzip.GzipFile(filename='',fileobj=raw,mode='wb',mtime=0) as compressed:
        with tarfile.open(fileobj=compressed,mode='w') as archive:
            for file in sorted(files):
                data=file.read_bytes()
                member=tarfile.TarInfo(file.relative_to(root/'Demo').as_posix())
                member.size=len(data)
                member.mode=0o755 if file.name=='run.sh' else 0o644
                archive.addfile(member,io.BytesIO(data))
    with tempfile.TemporaryDirectory(prefix='coregeek-verify-') as temp:
        with tarfile.open(output) as archive:
            archive.extractall(temp,filter='data')
        for file in files:
            unpacked=Path(temp)/file.relative_to(root/'Demo')
            assert unpacked.read_bytes()==file.read_bytes(), file
        assert len(list(Path(temp).rglob('*.*')))>=len(files)
    print(f'Verified {len(files)} files: {output}')
    print('SHA256',hashlib.sha256(output.read_bytes()).hexdigest())


if __name__=='__main__':
    main()
