"""Optional local NLI safety signal for the controlled P11 trial.

The adapter is deliberately lazy: importing this module never imports torch or
transformers.  Production remains unchanged while the feature is disabled.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading

MODEL_ID = "MoritzLaurer/multilingual-MiniLMv2-L6-mnli-xnli"
MODEL_REVISION = "0a71e92a985b6e1ad1828cf67ce9c459639c1dca"
CONTRACT_VERSION = "p11_semantic_nli_v1"
CONTRADICTION_THRESHOLD = 0.003467
MIN_ENTAILMENT_THRESHOLD = 0.437127


@dataclass(frozen=True)
class SemanticNLIResult:
    status: str
    reason: str = ""
    model_status: str = ""
    model_id: str = MODEL_ID
    model_revision: str = MODEL_REVISION
    forward: dict | None = None
    reverse: dict | None = None


def _eligible(source, candidate, group=None):
    if not str(source or "").strip() or not str(candidate or "").strip():
        return False
    if group is not None:
        if str(getattr(group, "classification", "unknown")) not in {"speech", "thought", "narration", "unknown"}:
            return False
        if getattr(group, "source_recovery_trusted", True) is False:
            return False
        if getattr(group, "detected_proper_names", ()) and len(str(source).split()) <= 2:
            return False
    return len(str(source).split()) >= 3 and len(str(candidate).split()) >= 3


class SemanticNLIAdapter:
    """Process-local, local-files-only scorer with injectable score support."""

    def __init__(self, *, enabled=False, model_path=None, scorer=None):
        self.enabled = bool(enabled)
        self.model_path = str(model_path or "")
        self.scorer = scorer
        self._model = None
        self._tokenizer = None
        self._lock = threading.Lock()

    def evaluate(self, source, candidate, *, group=None):
        if not self.enabled:
            return SemanticNLIResult("DISABLED", model_status="disabled")
        if not _eligible(source, candidate, group):
            return SemanticNLIResult("INELIGIBLE", model_status="ineligible")
        try:
            scores = self.scorer(source, candidate) if self.scorer else self._score(source, candidate)
            forward = scores["forward"]
            reverse = scores.get("reverse") or self.scorer(candidate, source)["forward"] if self.scorer else self._score(candidate, source)["forward"]
            max_c = max(float(forward["C"]), float(reverse["C"]))
            min_e = min(float(forward["E"]), float(reverse["E"]))
            if max_c >= CONTRADICTION_THRESHOLD and min_e <= MIN_ENTAILMENT_THRESHOLD:
                return SemanticNLIResult("STRONG_CONTRADICTION", "semantic_contradiction", "ready", forward=forward, reverse=reverse)
            return SemanticNLIResult("AMBIGUOUS", "", "ready", forward=forward, reverse=reverse)
        except Exception as exc:  # fail closed when enabled
            return SemanticNLIResult("UNAVAILABLE", f"semantic_check_unavailable:{type(exc).__name__}", "inference_error")

    def _load(self):
        if self._model is not None:
            return
        if not self.model_path or not Path(self.model_path).is_dir():
            raise FileNotFoundError("semantic_nli_model_missing")
        with self._lock:
            if self._model is not None:
                return
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
            self._tokenizer = AutoTokenizer.from_pretrained(self.model_path, local_files_only=True)
            self._model = AutoModelForSequenceClassification.from_pretrained(self.model_path, local_files_only=True).eval()

    def _score(self, premise, hypothesis):
        self._load()
        import torch
        encoded = self._tokenizer(premise, hypothesis, return_tensors="pt", truncation=True, max_length=256)
        with torch.inference_mode():
            probs = torch.softmax(self._model(**encoded).logits, dim=-1)[0].tolist()
        return {"forward": {"E": probs[0], "N": probs[1], "C": probs[2]}}

