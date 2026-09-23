"""Làm sạch văn bản tiếng Việt mà không đoán thêm chữ hoặc tự bịa dấu."""
import re
import unicodedata

def clean_text(text, replacements=None, max_chars=1400):
    text = unicodedata.normalize("NFC", str(text))
    text = "".join(ch for ch in text if ch in "\n\t" or not unicodedata.category(ch).startswith("C"))
    for old, new in (replacements or {}).items():
        if old:
            text = re.sub(r"(?<!\w)"+re.escape(old)+r"(?!\w)", lambda _: new, text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"([,;:!?])(?=[^\s\d])", r"\1 ", text)
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars].rsplit(" ", 1)[0] + "…"
    return result

def speech_chunks(text, limit=220):
    words, chunks, current = text.split(), [], ""
    for word in words:
        if len(current)+len(word)+1 > limit and current:
            chunks.append(current)
            current = ""
        current = (current+" "+word).strip()
    if current:
        chunks.append(current)
    return chunks
