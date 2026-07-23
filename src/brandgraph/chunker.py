"""Split cleaned document text into overlapping chunks for downstream retrieval."""
from __future__ import annotations

import re
from typing import Iterator

from .config import settings
from .models import Chunk, Document

_WS_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def _split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\n{2,}|\r\n{2,}", text) if p.strip()]
    return parts or [text.strip()]


def chunk_document(
    doc: Document,
    chunk_size: int | None = None,
    overlap: int | None = None,
) -> Iterator[Chunk]:
    """
    Yield ``Chunk`` objects for a document.

    Chunks respect paragraph boundaries where possible and target ``chunk_size``
    characters with ``overlap`` character carry-over between consecutive chunks.
    """
    size = chunk_size or settings.chunk_size
    ov = overlap if overlap is not None else settings.chunk_overlap
    if ov >= size:
        ov = size // 5

    paragraphs = _split_paragraphs(doc.text)

    buffer = ""
    position = 0
    for para in paragraphs:
        para = _normalise(para)
        if not para:
            continue
        candidate = f"{buffer}\n\n{para}".strip() if buffer else para
        if len(candidate) <= size:
            buffer = candidate
            continue

        # flush current buffer
        if buffer:
            yield Chunk(
                id=f"{doc.content_hash}-{position:04d}",
                document_url=doc.url,
                position=position,
                text=buffer,
            )
            position += 1
            tail = buffer[-ov:] if ov else ""
            buffer = f"{tail} {para}".strip()
        else:
            # paragraph alone is bigger than chunk_size — hard slice
            start = 0
            while start < len(para):
                slice_ = para[start : start + size]
                yield Chunk(
                    id=f"{doc.content_hash}-{position:04d}",
                    document_url=doc.url,
                    position=position,
                    text=slice_,
                )
                position += 1
                if start + size >= len(para):
                    break
                start += max(1, size - ov)
            buffer = ""

    if buffer:
        yield Chunk(
            id=f"{doc.content_hash}-{position:04d}",
            document_url=doc.url,
            position=position,
            text=buffer,
        )
