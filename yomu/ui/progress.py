import tkinter as tk
from tkinter import ttk
import ctypes
import time
import threading
import math

# -------------------------------------------
# Windows Acrylic / Blur (Mica/Acrylic)
# -------------------------------------------
def enable_blur_effect(hwnd):
    ACCENT_POLICY = 19
    class ACCENT(ctypes.Structure):
        _fields_ = [
            ("AccentState", ctypes.c_int),
            ("AccentFlags", ctypes.c_int),
            ("GradientColor", ctypes.c_int),
            ("AnimationId", ctypes.c_int)
        ]

    class WINCOMPATTR(ctypes.Structure):
        _fields_ = [
            ("Attribute", ctypes.c_int),
            ("Data", ctypes.POINTER(ACCENT)),
            ("SizeOfData", ctypes.c_size_t)
        ]

    accent = ACCENT()
    accent.AccentState = ACCENT_POLICY
    accent.GradientColor = 0xAAFFFFFF  # 40% translucidez

    data = WINCOMPATTR()
    data.Attribute = 19
    data.Data = ctypes.pointer(accent)
    data.SizeOfData = ctypes.sizeof(accent)

    ctypes.windll.user32.SetWindowCompositionAttribute(hwnd, ctypes.byref(data))


class ProgressWindow:
    def __init__(self, title="Processando...", icon_path=None):
        self.root = tk.Toplevel()
        self.root.title(title)
        self.root.geometry("480x180")
        self.root.overrideredirect(True)
        self.root.attributes("-alpha", 0.96)
        self.root.configure(bg="#F0F0F0")

        # Aplicar blur
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        enable_blur_effect(hwnd)

        # Centralizar
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w, h = 480, 180
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        # Ícone
        if icon_path:
            try:
                self.root.iconbitmap(icon_path)
            except:
                pass

        # Container
        cont = tk.Frame(self.root, bg="#FFFFFF")
        cont.place(relx=0.5, rely=0.5, anchor="center")

        # Label principal
        self.label = tk.Label(cont, text=title, font=("Segoe UI", 12, "bold"), bg="#FFFFFF")
        self.label.pack(pady=(10, 5))

        # Barra com gradiente animado
        self.canvas = tk.Canvas(cont, width=380, height=18, bg="#D0D0D0",
                                bd=0, highlightthickness=0, relief="ridge")
        self.canvas.pack(pady=5)
        self.bar = self.canvas.create_rectangle(0, 0, 0, 18, fill="#0078D4")

        # Percent + ETA + Speed
        self.sub_label = tk.Label(cont, text="", font=("Segoe UI", 9), fg="#555", bg="#FFFFFF")
        self.sub_label.pack(pady=2)

        # Animação de fade-in
        self.fade_in()

        # Controle de tempo para ETA e velocidade
        self.start_time = time.time()
        self.last_update = time.time()
        self.speed_buffer = []

    # ------------------------------------------------------------
    # Fade In
    # ------------------------------------------------------------
    def fade_in(self):
        for i in range(0, 96, 5):
            self.root.attributes("-alpha", i / 100)
            self.root.update()
            time.sleep(0.01)

    # ------------------------------------------------------------
    # Fade Out
    # ------------------------------------------------------------
    def fade_out(self):
        for i in range(96, -1, -4):
            self.root.attributes("-alpha", i / 100)
            self.root.update()
            time.sleep(0.01)

    # ------------------------------------------------------------
    # Atualizar barra / ETA / velocidade
    # ------------------------------------------------------------
    def update(self, current, total=None, message=None):
        # Mensagem
        if message:
            self.label.config(text=message)

        # Percentual
        if total:
            percent = (current / total) * 100
        else:
            percent = current

        percent = max(0, min(100, percent))

        # Animação gradiente
        full_width = 380
        self.canvas.coords(self.bar, 0, 0, full_width * (percent / 100), 18)

        # Velocidade (páginas por segundo)
        now = time.time()
        delta = now - self.last_update
        self.last_update = now

        if delta > 0:
            self.speed_buffer.append(1 / delta)
            if len(self.speed_buffer) > 10:
                self.speed_buffer.pop(0)

        speed = sum(self.speed_buffer) / len(self.speed_buffer) if self.speed_buffer else 0

        # ETA
        elapsed = now - self.start_time
        eta = (elapsed * (100 / percent - 1)) if percent > 1 else 0

        self.sub_label.config(
            text=f"{percent:.1f}%  •  ETA: {int(eta)}s  •  Velocidade: {speed:.2f} img/s"
        )

        self.root.update_idletasks()
        self.root.update()

    # ------------------------------------------------------------
    # Fechar com animação
    # ------------------------------------------------------------
    def close(self):
        try:
            self.fade_out()
            self.root.destroy()
        except:
            pass
