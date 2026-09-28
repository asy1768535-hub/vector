"""Guard invalid link actions in the isolated PDF recovery image only."""
from pathlib import Path


def guard_invalid_pdf_link_actions(source: str) -> str:
    old = (
        '        action = cast(DictionaryObject, link["/A"])\n'
        '        if action.get("/S") != "/GoTo":\n'
    )
    new = (
        '        action = cast(DictionaryObject, link["/A"])\n'
        '        if not isinstance(action, DictionaryObject):\n'
        '            return None\n'
        '        if action.get("/S") != "/GoTo":\n'
    )
    if source.count(old) != 1:
        raise RuntimeError("Unexpected pypdf link builder; refusing to patch")
    # Keep annotation dictionaries and appearance data intact; only an invalid
    # action is excluded from navigation reference resolution.
    return source.replace(old, new, 1)


if __name__ == "__main__":
    import pypdf.generic._link as links

    path = Path(links.__file__)
    patched = guard_invalid_pdf_link_actions(path.read_text(encoding="utf-8"))
    compile(patched, str(path), "exec")
    path.write_text(patched, encoding="utf-8")
