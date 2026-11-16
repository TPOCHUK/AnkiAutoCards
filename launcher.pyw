"""Report startup failures even when launched without a console."""
try:
    from app import App
    App().mainloop()
except Exception:
    import traceback
    import tkinter as tk
    from tkinter import messagebox
    from pathlib import Path
    import os
    folder = Path(os.getenv('APPDATA') or Path.home() / '.config') / 'AnkiAutoCards'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'startup-error.log').write_text(traceback.format_exc(), encoding='utf-8')
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror('Anki AutoCards', f'Не вдалося запустити програму. Деталі:\n{folder / "startup-error.log"}')
    root.destroy()
