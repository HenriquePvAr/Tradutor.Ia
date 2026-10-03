import json, sys, time
from pathlib import Path
import cv2, numpy as np, torch

ROOT = Path(r"C:\Users\henri\YomuExperimental\clean-final-model-gauntlet-v1")
REPO = ROOT / "third_party" / "contemporary_manga_text_seg"
WEIGHT = ROOT / "models" / "contemporary_manga_seg" / "model.pth"
MANIFEST = Path("reports/bakeoff_dataset_manifest.json")

def main():
    import segmentation_models_pytorch as smp
    import torch.nn as nn
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    item = next(x for x in data["items"] if x["technical_status"] == "PASS")
    img = cv2.imread(item["source"], cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(item["source"])
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    model = smp.UnetPlusPlus(encoder_name="tu-efficientnetv2_rw_m", encoder_weights=None,
                             in_channels=3, classes=1, activation=None, decoder_attention_type="scse")
    def convert(module):
        for name, child in list(module.named_children()):
            if isinstance(child, nn.BatchNorm2d):
                c = child.num_features
                g = next((n for n in range(min(8, c), 0, -1) if c % n == 0), 1)
                setattr(module, name, nn.GroupNorm(g, c))
            else:
                convert(child)
    convert(model.decoder)
    state = torch.load(WEIGHT, map_location="cpu")
    model.load_state_dict(state)
    model.eval()
    x = torch.from_numpy(rgb.transpose(2, 0, 1)).float() / 255.0
    x = (x - torch.tensor([0.485, 0.456, 0.406])[:, None, None]) / torch.tensor([0.229, 0.224, 0.225])[:, None, None]
    h, w = x.shape[1:]
    ph, pw = (32 - h % 32) % 32, (32 - w % 32) % 32
    x = torch.nn.functional.pad(x[None], (0, pw, 0, ph))
    t = time.perf_counter()
    with torch.no_grad():
        prob = model(x).sigmoid()[0, 0, :h, :w].numpy()
    elapsed = time.perf_counter() - t
    mask = (prob > 0.5).astype(np.uint8)
    print(json.dumps({"sample_id": item["sample_id"], "shape": [int(h), int(w)],
                      "elapsed_s": elapsed, "foreground_pixels": int(mask.sum()),
                      "prob_min": float(prob.min()), "prob_max": float(prob.max()),
                      "weight": str(WEIGHT)}, indent=2))

if __name__ == "__main__":
    main()
