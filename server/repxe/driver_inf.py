"""Select the catalog applicable to this project's AMD64 Windows packages."""
import re

MAX_INF_BYTES = 16 * 1024**2


def amd64_catalog(text):
    # CatalogFile platform decorations override the generic entry. Requiring
    # catalogs for other architectures rejects ordinary PnPUtil AMD64 exports.
    # https://learn.microsoft.com/windows-hardware/drivers/install/inf-version-section
    section, entries = '', {}
    for raw in text.splitlines():
        quoted = False
        for index, char in enumerate(raw):
            if char == '"':
                quoted = not quoted
            elif char == ';' and not quoted:
                raw = raw[:index]
                break
        line = raw.strip()
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1].strip().casefold()
            continue
        if section != 'version' or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip().casefold()
        if not re.fullmatch(r'catalogfile(?:\.[\w.]+)?', key):
            continue
        if key in entries:
            raise ValueError('Duplicate catalog directive in INF Version section')
        entries[key] = value.strip().strip('"')
    for key in ('catalogfile.ntamd64', 'catalogfile.nt', 'catalogfile'):
        if key in entries:
            value = entries[key]
            if not value:
                raise ValueError('AMD64 catalog reference is empty')
            return value
    raise ValueError('Every INF must declare an AMD64 or generic catalog')
