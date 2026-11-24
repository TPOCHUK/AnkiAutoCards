"""Windows desktop UI. All network work stays off the Tk main thread."""

from __future__ import annotations
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from uuid import uuid4
from dataclasses import fields
from pathlib import Path
import settings
from anki_core import AnkiConnect, read_words
from workflow import build_graph, error_message, list_models, validate_info
from rate_limits import format_rate_limit


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Anki AutoCards • English → Українська")
        self.geometry("1080x800")
        self.minsize(900, 680)
        self.configure(bg="#f3f5f8")
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.cards = {}
        self.serial = 0
        self.models = []
        self.token_records = []
        self.token_summary = tk.StringVar(value="Ще немає запитів до моделі.")
        self.rate_limit = None
        self.rate_limit_summary = tk.StringVar(value="Ліміти API: ще немає даних від провайдера.")
        try:
            s, key = settings.load()
        except Exception:
            s, key = settings.Settings(), ""
            self.after(
                400,
                lambda: messagebox.showwarning(
                    "Налаштування",
                    "Не вдалося прочитати налаштування. Імпортуй .env або введи їх знову.",
                ),
            )
        self.vars = {
            f.name: (
                tk.BooleanVar(value=getattr(s, f.name))
                if f.name == "audio"
                else tk.StringVar(value=getattr(s, f.name))
            )
            for f in fields(s)
        }
        self.key = tk.StringVar(value=key)
        self.remember = tk.BooleanVar(value=True)
        self.profiles = settings.load_profiles(s)
        current = next(p for p in self.profiles if p["id"] == s.profile_id)
        self.profile_name = tk.StringVar(value=current["name"])
        self.remember.set(current.get("remember", True))
        self.profile_keys = {s.profile_id: key}
        self.profile_limits = {}
        self.free_only = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="Встав слова → Створи перегляд → Перевір → Додай в Anki")
        self.progress = tk.DoubleVar(value=0)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f5f8")
        style.configure("TLabel", background="#f3f5f8", font=("Segoe UI", 10))
        style.configure("TButton", font=("Segoe UI", 10), padding=7)
        style.configure("Title.TLabel", font=("Segoe UI", 23, "bold"), foreground="#223c67")
        style.configure("Treeview", rowheight=30, font=("Segoe UI", 10))
        base = ttk.Frame(self, padding=20)
        base.pack(fill="both", expand=True)
        ttk.Label(base, text="Anki AutoCards", style="Title.TLabel").pack(anchor="w")
        ttk.Label(base, text="Слова, вирази, переклад і озвучка — в одному вікні.").pack(
            anchor="w", pady=(2, 12)
        )
        profile_bar = ttk.Frame(base)
        profile_bar.pack(fill="x", pady=(0, 10))
        ttk.Label(profile_bar, text="Профіль API:").pack(side="left")
        self.profile_box = ttk.Combobox(
            profile_bar,
            textvariable=self.profile_name,
            state="readonly",
            width=26,
            values=[p["name"] for p in self.profiles],
        )
        self.profile_box.pack(side="left", padx=8)
        self.profile_box.bind("<<ComboboxSelected>>", self.switch_profile)
        self.action(profile_bar, "Новий профіль", self.new_profile).pack(side="left")
        self.action(profile_bar, "Перейменувати", self.rename_profile).pack(side="left", padx=8)
        tabs = ttk.Notebook(base)
        tabs.pack(fill="both", expand=True)
        main = ttk.Frame(tabs, padding=12)
        pref = ttk.Frame(tabs, padding=15)
        tabs.add(main, text="  Картки  ")
        tabs.add(pref, text="  Налаштування  ")
        usage_tab = ttk.Frame(tabs, padding=12)
        tabs.add(usage_tab, text="  Токени  ")
        self.token_panel(usage_tab)
        top = ttk.Frame(main)
        top.pack(fill="x")
        ttk.Label(top, text="Колода:").pack(side="left")
        ttk.Entry(top, textvariable=self.vars["deck"], width=32).pack(side="left", padx=8)
        self.action(top, "Перевірити Anki", self.check_anki).pack(side="left")
        self.action(top, "Завантажити .txt", self.import_words).pack(side="right")
        ttk.Label(main, text="Одне слово або цілий вираз на рядок:").pack(anchor="w", pady=(12, 4))
        self.words = tk.Text(
            main,
            height=5,
            font=("Segoe UI", 11),
            relief="solid",
            bd=1,
            padx=10,
            pady=8,
            undo=True,
        )
        self.words.pack(fill="x")
        self.words.bind("<Control-KeyPress>", self.clipboard_shortcut)
        actions = ttk.Frame(main)
        actions.pack(fill="x", pady=10)
        self.action(actions, "1. Створити перегляд", self.preview).pack(side="left")
        self.action(actions, "2. Додати готові в Anki", self.add_cards).pack(side="left", padx=8)
        self.action(actions, "Прибрати вибрані", self.remove).pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Зупинити", command=self.stop, state="disabled")
        self.stop_btn.pack(side="right")
        ttk.Label(main, textvariable=self.rate_limit_summary, wraplength=780).pack(
            anchor="w", pady=(0, 8)
        )
        table = ttk.Frame(main)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            table,
            columns=("word", "meaning", "sentence", "status", "tokens"),
            show="headings",
            selectmode="extended",
            height=7,
        )
        for name, title, width in [
            ("word", "Слово / вираз", 160),
            ("meaning", "Переклад", 160),
            ("sentence", "Речення", 420),
            ("status", "Стан", 130),
            ("tokens", "Токени", 90),
        ]:
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=70)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.edit_card)
        ttk.Label(
            main,
            text="Подвійний клік по рядку — редагування. Додавання бере всі рядки зі станом «Готово».",
        ).pack(anchor="w", pady=5)
        self.log = tk.Text(
            main, height=4, font=("Consolas", 9), state="disabled", bg="#e8edf4", relief="flat"
        )
        self.log.pack(fill="x")
        self.preferences(pref)
        ttk.Progressbar(base, variable=self.progress, maximum=100).pack(fill="x", pady=(10, 5))
        ttk.Label(base, textvariable=self.status, wraplength=1000).pack(anchor="w")
        self.after(100, self.drain)
        self.after(1000, self.refresh_rate_limit)
        self.protocol("WM_DELETE_WINDOW", self.close)

    def action(self, parent, text, command):
        btn = ttk.Button(parent, text=text, command=command)
        if not hasattr(self, "buttons"):
            self.buttons = []
        self.buttons.append(btn)
        return btn

    @staticmethod
    def clipboard_shortcut(event):
        """Make clipboard shortcuts work with Latin and non-Latin layouts on Windows."""
        virtual_events = {
            "a": "<<SelectAll>>",
            "c": "<<Copy>>",
            "x": "<<Cut>>",
            "v": "<<Paste>>",
            "z": "<<Undo>>",
            65: "<<SelectAll>>",
            67: "<<Copy>>",
            88: "<<Cut>>",
            86: "<<Paste>>",
            90: "<<Undo>>",
        }
        virtual_event = virtual_events.get(event.keysym.lower()) or virtual_events.get(
            event.keycode
        )
        if virtual_event:
            event.widget.event_generate(virtual_event)
            return "break"
        return None

    def preferences(self, frame):
        frame.columnconfigure(1, weight=1)
        rows = [
            ("provider", "Провайдер"),
            ("base_url", "API Base URL"),
            ("model", "Модель (можна вписати ID)"),
            ("key", "API-ключ"),
            ("note_type", "Тип нотатки Anki"),
            ("anki_url", "AnkiConnect URL"),
            ("voice", "Голос озвучки"),
            ("language", "Мова перекладу"),
            ("max_tokens", "Максимум токенів"),
        ]
        for row, (name, label) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 15), pady=6)
            if name == "provider":
                widget = ttk.Combobox(
                    frame,
                    textvariable=self.vars[name],
                    values=["OpenRouter", "OpenAI", "OpenAI-compatible", "Anthropic"],
                    state="readonly",
                )
                widget.bind("<<ComboboxSelected>>", self.change_provider)
            elif name == "model":
                widget = self.model_box = ttk.Combobox(frame, textvariable=self.vars[name])
            elif name == "key":
                widget = self.key_entry = ttk.Entry(frame, textvariable=self.key, show="•")
            else:
                widget = ttk.Entry(frame, textvariable=self.vars[name])
            if name != "provider":
                widget.bind("<Control-KeyPress>", self.clipboard_shortcut)
            widget.grid(row=row, column=1, sticky="ew", pady=6)
        show = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame,
            text="Показати",
            variable=show,
            command=lambda: self.key_entry.configure(show="" if show.get() else "•"),
        ).grid(row=3, column=2, padx=8)
        models = ttk.Frame(frame)
        models.grid(row=2, column=2, padx=8)
        self.action(models, "Оновити список", self.refresh_models).pack()
        ttk.Checkbutton(frame, text="Лише безкоштовні (OpenRouter)", variable=self.free_only).grid(
            row=9, column=1, sticky="w", pady=5
        )
        ttk.Checkbutton(
            frame, text="Додавати озвучку слова та речення", variable=self.vars["audio"]
        ).grid(row=10, column=1, sticky="w", pady=5)
        ttk.Checkbutton(
            frame, text="Зберегти ключ у сховищі облікових даних ОС", variable=self.remember
        ).grid(row=11, column=1, sticky="w", pady=5)
        bar = ttk.Frame(frame)
        bar.grid(row=12, column=0, columnspan=3, sticky="w", pady=16)
        self.action(bar, "Зберегти", self.save_settings).pack(side="left")
        self.action(bar, "Імпортувати .env", self.import_env).pack(side="left", padx=8)
        self.action(bar, "Перевірити модель", self.test_model).pack(side="left")
        ttk.Label(
            frame,
            text="Зміни застосовуються до наступної операції.\n.env поруч із програмою підхоплюється при першому запуску. Його можна імпортувати вручну.\nКлюч не записується у settings.json. Безкоштовні моделі можуть мати ліміти або зникати.",
            wraplength=850,
        ).grid(row=13, column=0, columnspan=3, sticky="w", pady=10)

    def token_panel(self, frame):
        """Фактичні відповіді API за цей сеанс. Кожна спроба має свій рядок."""
        header = ttk.Frame(frame)
        header.pack(fill="x", pady=8)
        ttk.Label(header, textvariable=self.token_summary, wraplength=780).pack(
            side="left", anchor="w", fill="x", expand=True
        )
        self.action(header, "Очистити історію", self.clear_token_history).pack(side="right")
        ttk.Label(
            frame,
            text=(
                "Створи перегляд одного слова й подивись витрату тут або в колонці «Токени».\n"
                "Повторні спроби рахуються окремо. «Невідомо» означає, що API не повернув статистику.\n"
                "Список живе до закриття програми. Озвучка не входить у токени моделі."
            ),
            wraplength=950,
        ).pack(anchor="w", pady=8)
        columns = [
            ("source", "Операція", 100),
            ("word", "Слово", 140),
            ("model", "Модель", 170),
            ("attempt", "Спроба", 65),
            ("status", "Відповідь", 100),
            ("input", "Вхід", 85),
            ("output", "Вихід", 85),
            ("total", "Разом", 85),
        ]
        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True)
        self.token_tree = ttk.Treeview(table, columns=[row[0] for row in columns], show="headings")
        for name, title, width in columns:
            self.token_tree.heading(name, text=title)
            self.token_tree.column(name, width=width, minwidth=60)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.token_tree.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.token_tree.xview)
        self.token_tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.token_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")

    def clear_token_history(self):
        self.token_records.clear()
        rows = self.token_tree.get_children()
        if rows:
            self.token_tree.delete(*rows)
        self.token_summary.set("Ще немає запитів до моделі.")

    def publish_usage(self, result, s, source):
        # Workers send data; only drain() updates the table.
        if result.get("rate_limit"):
            self.events.put(
                ("rate_limit", dict(result["rate_limit"], provider=s.provider, model=s.model))
            )
        for record in result.get("usage_records", []):
            self.events.put(("usage", dict(record, model=s.model, source=source)))

    def show_usage(self, record):
        self.token_records.append(record)
        usage = record["usage"]
        counts = [
            usage[name] if usage is not None else "Невідомо"
            for name in ("input_tokens", "output_tokens", "total_tokens")
        ]
        status = {"valid": "Коректна", "invalid": "Некоректна", "failed": "Помилка"}[
            record["status"]
        ]
        iid = self.token_tree.insert(
            "",
            "end",
            values=(
                record["source"],
                record["word"],
                record["model"],
                record["attempt"],
                status,
                *counts,
            ),
        )
        self.token_tree.see(iid)
        known = [row["usage"] for row in self.token_records if row["usage"] is not None]
        totals = {
            name: sum(row[name] for row in known)
            for name in ("input_tokens", "output_tokens", "total_tokens")
        }
        missing = len(self.token_records) - len(known)
        self.token_summary.set(
            f"Відомі токени за сеанс: вхід {totals['input_tokens']}, вихід {totals['output_tokens']}, "
            f"разом {totals['total_tokens']}. Спроб: {len(self.token_records)}. "
            f"Без статистики: {missing}." + (" Сума неповна." if missing else "")
        )

    def snapshot(self, require_model=False):
        data = {k: v.get() for k, v in self.vars.items()}
        data["max_tokens"] = int(data["max_tokens"])
        for k, v in data.items():
            if isinstance(v, str):
                data[k] = v.strip()
        s = settings.Settings(**data)
        key = self.key.get().strip()
        s.validate(key, require_model=require_model)
        return s, key

    def change_provider(self, event=None):
        if self.busy:
            return
        provider = self.vars["provider"].get()
        defaults = {
            "OpenRouter": "https://openrouter.ai/api/v1",
            "OpenAI": "https://api.openai.com/v1",
            "Anthropic": "https://api.anthropic.com",
        }
        if provider in defaults:
            self.vars["base_url"].set(defaults[provider])
        self.vars["model"].set("")
        self.models = []
        self.model_box.configure(values=[])
        s = settings.Settings(provider=provider, base_url=self.vars["base_url"].get())
        self.key.set(settings.get_key(s) if self.vars["profile_id"].get() == "default" else "")

    def persist_profile(self):
        s, key = self.snapshot()
        profile = next(p for p in self.profiles if p["id"] == s.profile_id)
        profile.update({k: getattr(s, k) for k in settings.PROFILE_FIELDS})
        profile["remember"] = self.remember.get()
        message = settings.save(s, key, self.remember.get(), self.profiles)
        self.profile_keys[s.profile_id] = key
        return message

    def apply_profile(self, profile):
        old_id = self.vars["profile_id"].get()
        self.profile_limits[old_id] = self.rate_limit
        self.vars["profile_id"].set(profile["id"])
        for name in settings.PROFILE_FIELDS:
            self.vars[name].set(profile[name])
        s = settings.Settings(
            profile_id=profile["id"], provider=profile["provider"], base_url=profile["base_url"]
        )
        self.key.set(
            self.profile_keys[profile["id"]]
            if profile["id"] in self.profile_keys
            else settings.get_key(s)
        )
        self.remember.set(profile.get("remember", True))
        self.profile_name.set(profile["name"])
        self.profile_box.configure(values=[p["name"] for p in self.profiles])
        self.models = []
        self.model_box.configure(values=[])
        self.rate_limit = self.profile_limits.get(profile["id"])
        self.rate_limit_summary.set(
            format_rate_limit(self.rate_limit)
            if self.rate_limit
            else "Ліміти API: ще немає даних від провайдера для цього профілю."
        )

    def switch_profile(self, event=None):
        current = next(p for p in self.profiles if p["id"] == self.vars["profile_id"].get())
        if self.busy:
            self.profile_name.set(current["name"])
            return
        target = next(p for p in self.profiles if p["name"] == self.profile_name.get())
        try:
            message = self.persist_profile()
            self.apply_profile(target)
            target_message = self.persist_profile()
            messages = " ".join(dict.fromkeys((message, target_message)))
            self.write_log(f"Обрано профіль: {target['name']}. {messages}")
        except Exception as exc:
            if self.vars["profile_id"].get() != current["id"]:
                self.apply_profile(current)
            self.profile_name.set(current["name"])
            messagebox.showerror("Профілі", error_message(exc, self.key.get()))

    def ask_profile_name(self, initial=""):
        name = simpledialog.askstring(
            "Профіль API", "Назва профілю:", initialvalue=initial, parent=self
        )
        if name is None:
            return None
        name = name.strip()
        if not name or any(
            p["name"].casefold() == name.casefold() and p["name"] != initial for p in self.profiles
        ):
            messagebox.showerror("Профілі", "Вкажи непорожню унікальну назву профілю.")
            return None
        return name

    def new_profile(self):
        if self.busy:
            return
        name = self.ask_profile_name()
        if name is None:
            return
        try:
            message = self.persist_profile()
            defaults = settings.Settings()
            profile = dict(
                id=uuid4().hex,
                name=name,
                remember=True,
                **{k: getattr(defaults, k) for k in settings.PROFILE_FIELDS},
            )
            self.profiles.append(profile)
            self.profile_keys[profile["id"]] = ""
            self.apply_profile(profile)
            target_message = self.persist_profile()
            messages = " ".join(dict.fromkeys((message, target_message)))
            self.write_log(
                f"Створено профіль «{name}». Введи URL, ключ і модель у налаштуваннях. {messages}"
            )
        except Exception as exc:
            messagebox.showerror("Профілі", error_message(exc, self.key.get()))

    def rename_profile(self):
        if self.busy:
            return
        name = self.ask_profile_name(self.profile_name.get())
        if name is None:
            return
        profile = next(p for p in self.profiles if p["id"] == self.vars["profile_id"].get())
        old_name = profile["name"]
        profile["name"] = name
        try:
            self.status.set(self.persist_profile())
            self.profile_name.set(name)
            self.profile_box.configure(values=[p["name"] for p in self.profiles])
        except Exception as exc:
            profile["name"] = old_name
            messagebox.showerror("Профілі", error_message(exc, self.key.get()))

    def save_settings(self):
        try:
            self.status.set(self.persist_profile())
        except Exception as exc:
            messagebox.showerror("Налаштування", error_message(exc, self.key.get()))

    def import_env(self):
        path = filedialog.askopenfilename(
            title="Вибери свій .env", initialdir=settings.ROOT, filetypes=[("Усі файли", "*")]
        )
        if path:
            try:
                s, key = settings.import_env(path)
                for f in fields(s):
                    if f.name != "profile_id":
                        self.vars[f.name].set(getattr(s, f.name))
                self.key.set(key)
                self.status.set(
                    ".env імпортовано. Натисни «Зберегти», щоб запам’ятати налаштування."
                )
            except Exception as exc:
                messagebox.showerror("Імпорт", error_message(exc, self.key.get()))

    def import_words(self):
        path = filedialog.askopenfilename(filetypes=[("Список слів", "*.txt"), ("Усі файли", "*")])
        if path:
            try:
                text = Path(path).read_text(encoding="utf-8-sig")
                self.words.insert("end", "\n" + text)
            except Exception as exc:
                messagebox.showerror("Файл", str(exc))

    def start_job(self, work):
        if self.busy:
            return
        self.busy = True
        self.profile_box.configure(state="disabled")
        self.cancel.clear()
        self.progress.set(0)
        for btn in self.buttons:
            btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        # Only the main thread can use Tk widgets.
        key = self.key.get().strip()

        def worker():
            try:
                work()
            except Exception as exc:
                self.events.put(("error", error_message(exc, key)))
            finally:
                self.events.put(("done", None))

        threading.Thread(target=worker, daemon=True).start()

    def check_anki(self):
        try:
            s, _ = self.snapshot()
        except Exception as exc:
            messagebox.showerror("Налаштування", str(exc))
            return

        def work():
            api = AnkiConnect(s.anki_url, model=s.note_type)
            api.invoke("version")
            decks = api.invoke("deckNames")
            self.events.put(("log", "Anki підключено. Колоди: " + ", ".join(decks)))

        self.start_job(work)

    def refresh_models(self):
        try:
            s, key = self.snapshot()
        except Exception as exc:
            messagebox.showerror("Налаштування", str(exc))
            return
        free = self.free_only.get()
        self.start_job(lambda: self.events.put(("models", list_models(s, key, free))))

    def test_model(self):
        try:
            s, key = self.snapshot(True)
        except Exception as exc:
            messagebox.showerror("Налаштування", str(exc))
            return

        def work():
            workflow = build_graph(
                s,
                key,
                None,
                notify=lambda msg: self.events.put(("log", msg)),
                stopped=self.cancel.is_set,
            )
            result = workflow.invoke({"word": "thrive", "check_anki": False})
            self.publish_usage(result, s, "Тест моделі")
            if result["status"] == "cancelled":
                return
            if result["status"] != "ready":
                raise ValueError(result["error"])
            info = result["info"]
            self.events.put(("log", "Модель працює: " + info.sentence + " → " + info.translation))

        self.start_job(work)

    def preview(self):
        """Генеруємо текст у фоні. До Anki тут нічого не записуємо."""
        try:
            s, key = self.snapshot(True)
            words = read_words(self.words.get("1.0", "end").splitlines())
            if not words:
                raise ValueError("Спочатку встав слова або завантаж .txt.")
        except Exception as exc:
            messagebox.showerror("Перегляд", str(exc))
            return

        def work():
            # Use Anki for duplicate checks when it is available; preview also works offline.
            api = AnkiConnect(s.anki_url, model=s.note_type)
            try:
                api.invoke("version")
                check_anki = True
            except Exception:
                api, check_anki = None, False
                self.events.put(
                    (
                        "log",
                        "Anki недоступний: перегляд без перевірки дублікатів. Перевірю перед додаванням.",
                    )
                )
            workflow = build_graph(
                s,
                key,
                api,
                notify=lambda msg: self.events.put(("log", msg)),
                stopped=self.cancel.is_set,
            )
            for index, word in enumerate(words):
                if self.cancel.is_set():
                    break
                result = workflow.invoke(
                    {"word": word, "mode": "preview", "check_anki": check_anki}
                )
                self.publish_usage(result, s, "Перегляд")
                self.events.put(("card", result))
                self.events.put(("progress", 100 * (index + 1) / len(words)))
                if result.get("rate_limit"):
                    self.events.put(
                        ("log", "Перегляд зупинено через ліміт API. Решта слів не оброблялася.")
                    )
                    break

        self.start_job(work)

    def add_cards(self):
        ready = [
            (iid, dict(state))
            for iid, state in self.cards.items()
            if state["status"] in ("ready", "failed", "cancelled") and "info" in state
        ]
        if not ready:
            messagebox.showinfo("Anki", "Немає готових карток. Спочатку створи перегляд.")
            return
        try:
            s, key = self.snapshot()
        except Exception as exc:
            messagebox.showerror("Налаштування", str(exc))
            return

        def work():
            api = AnkiConnect(s.anki_url, model=s.note_type)
            api.invoke("version")
            api.ensure_model()
            api.ensure_deck(s.deck)
            workflow = build_graph(
                s,
                key,
                api,
                notify=lambda msg: self.events.put(("log", msg)),
                stopped=self.cancel.is_set,
            )
            for index, (iid, state) in enumerate(ready):
                if self.cancel.is_set():
                    break
                # Retry adding without calling the model.
                result = workflow.invoke(
                    {
                        "word": state["word"],
                        "info": state["info"],
                        "mode": "add",
                        "check_anki": True,
                    }
                )
                # Adding a preview keeps its generation totals visible.
                for name in ("usage", "usage_requests", "attempts", "usage_records"):
                    if name in state:
                        result[name] = state[name]
                self.events.put(("update", (iid, result)))
                self.events.put(("progress", 100 * (index + 1) / len(ready)))

        self.start_job(work)

    def render_card(self, iid, state):
        info = state.get("info")
        label = {
            "ready": "Готово",
            "added": "Додано",
            "duplicate": "Дублікат",
            "failed": "Помилка",
            "invalid": "Некоректна відповідь",
            "cancelled": "Зупинено",
        }.get(state["status"], state["status"])
        values = (
            state["word"],
            info.translation if info else "",
            info.sentence if info else "",
            label,
            self.card_tokens(state),
        )
        if self.tree.exists(iid):
            self.tree.item(iid, values=values)
        else:
            self.tree.insert("", "end", iid=iid, values=values)
        if state.get("error"):
            self.write_log(state["word"] + ": " + state["error"])

    @staticmethod
    def card_tokens(state):
        attempts = state.get("attempts", 0)
        known = state.get("usage_requests", 0)
        if not attempts:
            return "0"
        if not known:
            return "Невідомо"
        total = str(state["usage"]["total_tokens"])
        return total + ("+" if known < attempts else "")

    def remove(self):
        if self.busy:
            return
        for iid in self.tree.selection():
            self.tree.delete(iid)
            self.cards.pop(iid, None)

    def edit_card(self, event=None):
        if self.busy or not self.tree.selection():
            return
        iid = self.tree.selection()[0]
        state = self.cards[iid]
        if "info" not in state or state["status"] in ("added", "duplicate"):
            return
        info = state["info"]
        dialog = tk.Toplevel(self)
        dialog.title("Редагувати картку")
        dialog.geometry("660x300")
        dialog.transient(self)
        dialog.grab_set()
        values = {}
        for row, (name, label) in enumerate(
            [
                ("lemma", "Слово"),
                ("form", "Форма у реченні"),
                ("translation", "Переклад"),
                ("sentence", "Речення"),
            ]
        ):
            ttk.Label(dialog, text=label).grid(row=row, column=0, padx=12, pady=12)
            values[name] = tk.StringVar(value=getattr(info, name))
            ttk.Entry(dialog, textvariable=values[name], width=62).grid(row=row, column=1, padx=12)

        def commit():
            try:
                updated = validate_info({k: v.get() for k, v in values.items()}, state["word"])
                state.update(info=updated, status="ready", error="")
                self.render_card(iid, state)
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror("Картка", str(exc), parent=dialog)

        ttk.Button(dialog, text="Зберегти", command=commit).grid(row=4, column=1, pady=10)

    def write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.status.set(text)

    def refresh_rate_limit(self):
        if self.rate_limit is not None:
            info = self.rate_limit
            self.rate_limit_summary.set(
                f"{info['provider']} / {info['model']}: " + format_rate_limit(info)
            )
        self.after(1000, self.refresh_rate_limit)

    def drain(self):
        """Забираємо події з черги й оновлюємо вікно в головному потоці."""
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    self.write_log(value)
                elif kind == "usage":
                    self.show_usage(value)
                elif kind == "rate_limit":
                    self.rate_limit = value
                    self.rate_limit_summary.set(
                        f"{value['provider']} / {value['model']}: " + format_rate_limit(value)
                    )
                elif kind == "error":
                    self.write_log(value)
                    messagebox.showerror("Операція не завершена", value)
                elif kind == "progress":
                    self.progress.set(value)
                elif kind == "models":
                    self.models = value
                    self.model_box.configure(values=value)
                    if value:
                        if self.vars["model"].get() not in value:
                            self.vars["model"].set(value[0])
                        self.write_log(
                            f"Знайдено моделей: {len(value)}. Вибери потрібну в полі «Модель»."
                        )
                    else:
                        self.write_log("Список порожній. Зміни фільтр або введи ID моделі вручну.")
                elif kind == "card":
                    self.serial += 1
                    iid = str(self.serial)
                    self.cards[iid] = value
                    self.render_card(iid, value)
                elif kind == "update":
                    iid, state = value
                    self.cards[iid] = state
                    self.render_card(iid, state)
                elif kind == "done":
                    self.busy = False
                    self.profile_box.configure(state="readonly")
                    for btn in self.buttons:
                        btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    counts = {
                        s: sum(c["status"] == s for c in self.cards.values())
                        for s in ("ready", "added", "duplicate", "failed", "invalid")
                    }
                    self.write_log(
                        ("Зупинено. " if self.cancel.is_set() else "Операцію завершено. ")
                        + f"Готово: {counts['ready']} · Додано: {counts['added']} · Дублікати: {counts['duplicate']} · Помилки: {counts['failed'] + counts['invalid']}"
                    )
        except queue.Empty:
            pass
        self.after(100, self.drain)

    def stop(self):
        self.cancel.set()
        self.status.set("Зупиняю після поточного мережевого запиту…")

    def close(self):
        if self.busy:
            self.stop()
            self.status.set("Завершую поточний запит. Після зупинки вікно можна закрити.")
            return
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
