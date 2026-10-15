"""Provision a private stable log-ID HMAC key; never print or overwrite it."""

import os
import secrets
import stat
from pathlib import Path


def main():
    root = Path.home() / ".local/state/sisu-kudos"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = root.lstat()
    assert stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
    assert not info.st_mode & 0o077, "key directory must be private; refusing permission changes"
    path = root / "log-identity.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        created = False
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(secrets.token_bytes(32))
            stream.flush()
            os.fsync(stream.fileno())
        created = True
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
    assert info.st_nlink == 1 and not info.st_mode & 0o077 and info.st_size == 32
    print(f"Private log identity key verified (created={created}, mode=0600, bytes=32)")


if __name__ == "__main__":
    main()
