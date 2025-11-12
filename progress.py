import tkinter as tk
from tkinter import ttk

class ProgressWindow:
    def __init__(self, title="Progresso"):
        self.root = tk.Toplevel()
        self.root.title(title)
        self.root.geometry("400x100")
        self.label = tk.Label(self.root, text="")
        self.label.pack(pady=10)
        self.progress = ttk.Progressbar(self.root, length=350, mode="determinate")
        self.progress.pack(pady=10)
        self.root.update()

    def update(self, value, maximum, text=""):
        self.progress["maximum"] = maximum
        self.progress["value"] = value
        self.label.config(text=text)
        self.root.update()

    def close(self):
        self.root.destroy()
