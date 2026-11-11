"""Import clearly fictional, dated examples for real model integration checks."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from searchos.local.library import LocalLibraryService

ROOT = Path(__file__).resolve().parents[1]
NAME = "创新模块演示 · 虚构任职资料"


async def main() -> None:
    service = LocalLibraryService(ROOT / "knowledge_base")
    library = next((item for item in service.list_libraries() if item.name == NAME), None)
    if library is not None:
        print(f"演示资料库已存在：{library.library_id}")
        return
    library = service.create_library(NAME)
    documents = []
    for source in sorted((ROOT / "examples/innovation").glob("*.md")):
        with tempfile.TemporaryDirectory() as temporary:
            staged = Path(temporary) / source.name
            shutil.copy2(source, staged)
            document = await service.index_uploaded_file(library.library_id, staged, source.name)
        service._verified_blocks(library.library_id, [document["document_id"]])
        documents.append({"document_id": document["document_id"], "file": source.name})
    output = ROOT / "data/innovation-library.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({"library_id": library.library_id, "documents": documents},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已导入并校验 {len(documents)} 份虚构演示资料：{library.library_id}")


if __name__ == "__main__":
    asyncio.run(main())
