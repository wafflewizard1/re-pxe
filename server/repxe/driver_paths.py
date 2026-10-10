"""Portable relative filenames for Windows driver packages."""
import re


def validate(name: str, directory: bool = False) -> str:
    path = name[:-1] if directory and name.endswith('/') else name
    try:
        units = lambda value: len(value.encode('utf-16-le')) // 2
        if (not path or units(path) > 32700 or
                any(ord(c) < 32 or c in '\\:<>"|?*' for c in path)):
            raise ValueError
        for part in path.split('/'):
            stem = part.split('.', 1)[0].rstrip(' ')
            if (part in ('', '.', '..') or part.endswith((' ', '.')) or units(part) > 255 or
                    re.fullmatch(r'(?i)(con|prn|aux|nul|(?:com|lpt)[1-9¹²³])', stem)):
                raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError('Unsafe driver archive path: ' + repr(name)) from None
    return path
