"""Code copied into a Kaggle GPU notebook by the lab runner."""
from pathlib import Path
import hashlib, json, os, time

MODEL = "Qwen/Qwen3-VL-8B-Instruct"

def main():
    image = Path(os.environ.get("QWEN_IMAGE", next(str(p) for p in Path("/kaggle/input").rglob("input.*"))))
    prompt = os.environ.get("QWEN_PROMPT", next(Path(p).read_text(encoding="utf-8") for p in Path("/kaggle/input").rglob("prompt.txt")))
    started = time.time()
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    from transformers import Qwen3VLForConditionalGeneration, AutoProcessor
    processor = AutoProcessor.from_pretrained(MODEL)
    model = Qwen3VLForConditionalGeneration.from_pretrained(
        MODEL, torch_dtype="auto", device_map="auto"
    )
    messages = [{"role":"user", "content":[{"type":"image", "image":str(image)}, {"type":"text", "text":prompt}]}]
    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = processor(text=[text], images=[str(image)], return_tensors="pt", padding=True)
    inputs = {k: v.to(model.device) if hasattr(v, "to") else v for k, v in inputs.items()}
    output = model.generate(**inputs, max_new_tokens=512)
    answer = processor.batch_decode(output[:, inputs["input_ids"].shape[1]:], skip_special_tokens=True)[0]
    result = {"schema_version":"1", "status":"ok", "image_sha256":digest, "model":MODEL,
              "prompt":prompt, "answer":answer, "runtime":{"seconds":round(time.time()-started, 3)}}
    Path("/kaggle/working/result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

if __name__ == "__main__": main()
