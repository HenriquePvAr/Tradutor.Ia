from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Protocol, Any
import hashlib, json

class JobState(str, Enum):
    PENDING="PENDING"; PROCESSING="PROCESSING"; GENERATED="GENERATED"; VALIDATING="VALIDATING"
    COMPLETED="COMPLETED"; REVIEW_REQUIRED="REVIEW_REQUIRED"; FAILED="FAILED"

@dataclass
class HardCleanJob:
    job_id: str; page_id: str; region_id: str; content_type: str; erase_eligible: bool
    target_text: str; input_path: str; source_sha256: str; authorized_region: dict
    prompt_profile: str = "remove_text_preserve_art_v1"; attempt: int = 0; state: str = JobState.PENDING.value

    def to_dict(self): return asdict(self)
    @classmethod
    def from_path(cls, path): return cls(**json.loads(Path(path).read_text(encoding="utf-8")))
    @staticmethod
    def sha256(path):
        h=hashlib.sha256()
        with open(path,"rb") as f:
            for chunk in iter(lambda:f.read(1024*1024),b""): h.update(chunk)
        return h.hexdigest()

class HardCleanProvider(Protocol):
    def health(self) -> dict: ...
    def clean_text_region(self, image: Path, target_text: str, region: dict, prompt_profile: str, job_id: str) -> Path: ...

PROMPT_PROFILE = "Remova o texto em inglês desta imagem, preservando a arte original. Devolva a mesma imagem sem o texto e sem adicionar texto substituto."
PROMPT_PROFILE_ID = "REMOVE_ENGLISH_TEXT_PRESERVE_ART_V3"
