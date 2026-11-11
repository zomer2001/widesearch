"""Import bundled public examples using the current parser, without model calls."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from searchos.local.index.source_registry import SourceRegistry
from searchos.local.library import LocalLibraryService

ROOT = Path(__file__).resolve().parents[1]


async def main() -> None:
    service = LocalLibraryService(ROOT / "knowledge_base")
    manifest = json.loads((ROOT / "examples/anduril/manifest.json").read_text())
    previous = next((item for item in service.list_libraries() if item.name == manifest["name"]), None)
    if previous is not None:
        print(f"示例资料库已存在：{previous.library_id}")
        return
    library = service.create_library(manifest["name"])
    documents = []
    for item in manifest["files"]:
        source = ROOT / "examples/anduril" / item["file"]
        with tempfile.TemporaryDirectory() as temporary:
            staged = Path(temporary) / source.name
            shutil.copy2(source, staged)
            document = await service.index_uploaded_file(library.library_id, staged, source.name)
        registry = SourceRegistry(Path(library.index_root))
        record = registry.read(document["document_id"])
        if item.get("origin"):
            registry.write(record.model_copy(update={"origin": item["origin"]}))
        # Verify the parsed artifact, original hash, scope and usable blocks.
        service._verified_blocks(library.library_id, [document["document_id"]])
        documents.append({"document_id": document["document_id"], "file": source.name})
    output = ROOT / "data/demo-library.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({"library_id": library.library_id, "documents": documents}, ensure_ascii=False, indent=2))
    print(f"已导入并校验 {len(documents)} 篇公开资料，库 ID：{library.library_id}")


if __name__ == "__main__":
    asyncio.run(main())
