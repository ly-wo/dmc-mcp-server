"""Load credentials locally; never accept secrets as MCP tool arguments."""
import json
import os
import stat
from pathlib import Path

from .client import InstanceCredentials


def load_credentials() -> tuple[str, int, dict[str, InstanceCredentials]]:
    path = os.environ.get('DMC_CREDENTIALS_FILE')
    if not path or not Path(path).is_absolute():
        raise ValueError('Set DMC_CREDENTIALS_FILE to an absolute private JSON file path.')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Credentials must be an owner-only regular file (chmod 600).')
        with os.fdopen(fd, 'r', encoding='utf-8', closefd=False) as stream:
            raw = stream.read(65_537)
        if len(raw) > 65_536:
            raise ValueError('Credentials file is too large.')
        data = json.loads(raw)
        cookie, csrf, profiles = data['cookie'], data['mc_gtk'], data['profiles']
        if not isinstance(cookie, str) or not cookie.strip() or '\n' in cookie or '\r' in cookie:
            raise ValueError('Invalid cookie.')
        if type(csrf) is not int or csrf < 0 or not isinstance(profiles, dict) or not profiles:
            raise ValueError('Invalid credentials configuration.')
        result = {}
        for alias, values in profiles.items():
            if not isinstance(alias, str) or not alias or not isinstance(values, dict):
                raise ValueError('Invalid profile.')
            cred = InstanceCredentials(**values)
            if not all(isinstance(v, str) and v for v in (cred.instance_id, cred.user, cred.password)):
                raise ValueError('Invalid profile credentials.')
            if cred.db_type not in {'cynosdbmysql', 'tdsql'} or type(cred.region_id) is not int or cred.region_id <= 0:
                raise ValueError('Unsupported database type or region.')
            result[alias] = cred
        return cookie, csrf, result
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise ValueError('Invalid credentials file; check its structure and owner-only permissions.') from None
    finally:
        os.close(fd)
