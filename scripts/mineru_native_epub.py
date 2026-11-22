"""Render MinerU's own EPUB (RenderFormat.EPUB) from middle_json.json for comparison with the pandoc route.

  /opt/ocr/mineru/bin/python mineru_native_epub.py <raw_dir> <out.epub> <title>
"""
import sys
import json
import pathlib
from mineru.types import MiddleJson
from mineru.render.api import render
from mineru.render.contracts import RenderFormat, EpubRenderOptions

raw = pathlib.Path(sys.argv[1])
mj = MiddleJson.model_validate(json.loads((raw / "middle_json.json").read_text(encoding="utf-8")))


def resolve(path: str) -> bytes:
    return (raw / path).read_bytes()


data = render(mj, RenderFormat.EPUB, options=EpubRenderOptions(title=sys.argv[3], language="zh-CN",
                                                                asset_resolver=resolve))
pathlib.Path(sys.argv[2]).write_bytes(data)
print(sys.argv[2], len(data), "bytes")
