from pathlib import Path
from PIL import Image
import shutil
from .contracts import PROMPT_PROFILE

class MockHardCleanProvider:
    def __init__(self, output_dir): self.output_dir=Path(output_dir); self.calls=[]
    def health(self): return {"provider":"mock","status":"WORKING"}
    def clean_text_region(self,image,target_text,region,prompt_profile,job_id):
        self.calls.append((job_id,target_text,region)); out=self.output_dir/f"{job_id}.png"; out.parent.mkdir(parents=True,exist_ok=True)
        im=Image.open(image).convert("RGBA"); px=im.load(); x,y,w,h=[int(region[k]) for k in ("x","y","width","height")]
        for yy in range(max(0,y),min(im.height,y+h)):
            for xx in range(max(0,x),min(im.width,x+w)): px[xx,yy]=(255,255,255,255)
        im.save(out); return out

class GeminiWebProvider:
    """Playwright adapter boundary. Never automates credentials/MFA/CAPTCHA."""
    def __init__(self, profile, headed=False): self.profile=Path(profile); self.headed=headed
    def health(self): return {"provider":"gemini_web","status":"MANUAL_LOGIN_REQUIRED","profile":str(self.profile)}
    def clean_text_region(self,*args,**kwargs): raise RuntimeError("AUTH_INTERACTION_REQUIRED: run headed session and authenticate legitimately")
