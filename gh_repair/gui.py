"""customtkinter-Oberflaeche fuer das GREYHOUND Archive Repair Tool.

Modernes Design (Dark/Light, abgerundete Ecken) analog zum Website-Scraper-
Projekt. Benoetigt das Paket ``customtkinter`` (siehe requirements.txt).
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

from . import __version__, engine, updater
from .settings import load_settings as _load_settings
from .settings import save_settings as _save_settings

# Akzent-/Statusfarben (Tuple = (Light, Dark))
COL_OK = ("#15803d", "#4ade80")
COL_WARN = ("#b45309", "#fbbf24")
COL_MUTED = "gray55"
COL_DANGER = ("#b91c1c", "#ef4444")
COL_LOGBG = ("gray96", "#141420")
COL_LOGFG = ("gray10", "#d4d4d4")


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"GREYHOUND Archive Repair Tool   v{__version__}")
        self.geometry("900x740")
        self.update_idletasks()
        x = (self.winfo_screenwidth() - 900) // 2
        y = (self.winfo_screenheight() - 740) // 2
        self.geometry(f"900x740+{x}+{y}")
        self.minsize(720, 600)

        self._log_queue: queue.Queue = queue.Queue()
        self._busy = False
        self._merge_sources: list[str] = []
        self._backup_var = tk.BooleanVar(
            value=_load_settings().get("backup_originals", True))

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        self._build_header()
        self._build_tabs()
        self._build_log_area()

        self.after(120, self._drain_log)
        self.after(3000, self._check_update)  # nicht-blockierend
        self.after(1500, self._load_changelog)

    # ------------------------------------------------------------------ Header
    def _build_header(self) -> None:
        hdr = ctk.CTkFrame(self, corner_radius=0, fg_color=("gray88", "gray14"))
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            hdr, text="🛠   GREYHOUND Archive Repair Tool",
            font=ctk.CTkFont(size=20, weight="bold"), anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=22, pady=(14, 2))
        ctk.CTkLabel(
            hdr,
            text="Defekte Archive reparieren · mehrere Archive verlustfrei "
            "zusammenführen",
            font=ctk.CTkFont(size=12), text_color=COL_MUTED, anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=22, pady=(0, 14))

        appearance = _load_settings().get("appearance", "Dunkel")
        self._appear_var = tk.StringVar(value=appearance)
        ctk.CTkOptionMenu(
            hdr, values=["System", "Hell", "Dunkel"], width=120,
            variable=self._appear_var, command=self._change_appearance,
        ).grid(row=0, column=1, rowspan=2, padx=(0, 20))

    def _change_appearance(self, choice: str) -> None:
        ctk.set_appearance_mode({"System": "system", "Hell": "light",
                                 "Dunkel": "dark"}.get(choice, "dark"))
        s = _load_settings()
        s["appearance"] = choice
        _save_settings(s)

    # -------------------------------------------------------------------- Tabs
    def _build_tabs(self) -> None:
        self.tabs = ctk.CTkTabview(self, height=300)
        self.tabs.grid(row=1, column=0, sticky="ew", padx=18, pady=(14, 6))
        self._build_repair_tab(self.tabs.add("  🔧  Reparieren  "))
        self._build_merge_tab(self.tabs.add("  🔀  Zusammenführen  "))
        self._build_changelog_tab(self.tabs.add("  📰  Changelog  "))

    def _build_repair_tab(self, tab) -> None:
        tab.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            tab, justify="left", text_color=COL_MUTED,
            font=ctk.CTkFont(size=12), anchor="w",
            text="Repariert eine einzelne (defekte) archive.db3: Volltextindex "
            "neu aufbauen, Statistik\nneu berechnen, beschädigte DB "
            "wiederherstellen, Datei↔DB abgleichen. Erzeugt eine\nreparierte "
            "Kopie – die Quelle bleibt unangetastet.",
        ).grid(row=0, column=0, sticky="w", padx=6, pady=(8, 12))

        self.repair_src = self._folder_row(tab, 1, "Defektes Archiv")
        self.repair_dst = self._folder_row(tab, 2, "Ziel (Reparatur-Kopie)")

        self.repair_btn = ctk.CTkButton(
            tab, text="▶   Reparatur starten", height=40,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._start_repair,
        )
        self.repair_btn.grid(row=3, column=0, sticky="e", padx=6, pady=(14, 8))

    def _build_merge_tab(self, tab) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        ctk.CTkLabel(
            tab, justify="left", text_color=COL_MUTED,
            font=ctk.CTkFont(size=12), anchor="w",
            text="Führt mehrere Archive verlustfrei zu einem zusammen. "
            "Kollidierende Item-IDs werden\nautomatisch neu nummeriert, .eml "
            "umbenannt und alle Verweise mitgezogen.",
        ).grid(row=0, column=0, sticky="w", padx=6, pady=(8, 8))

        self._src_frame = ctk.CTkScrollableFrame(
            tab, label_text="Quell-Archive", height=150
        )
        self._src_frame.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 8))
        self._src_frame.columnconfigure(0, weight=1)
        self._render_sources()

        btns = ctk.CTkFrame(tab, fg_color="transparent")
        btns.grid(row=2, column=0, sticky="ew", padx=6)
        ctk.CTkButton(btns, text="➕  Archiv hinzufügen", height=34,
                      command=self._add_merge_source).pack(side="left")

        self.merge_dst = self._folder_row(tab, 3, "Ziel (neues Gesamtarchiv)")

        self.merge_btn = ctk.CTkButton(
            tab, text="▶   Zusammenführen starten", height=40,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._start_merge,
        )
        self.merge_btn.grid(row=4, column=0, sticky="e", padx=6, pady=(14, 8))

    def _build_changelog_tab(self, tab) -> None:
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)
        hdr = ctk.CTkFrame(tab, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=6, pady=(8, 4))
        hdr.columnconfigure(0, weight=1)
        self._clog_status = tk.StringVar(value=f"Version {__version__}")
        ctk.CTkLabel(hdr, textvariable=self._clog_status, anchor="w",
                     font=ctk.CTkFont(size=11), text_color=COL_MUTED).grid(
            row=0, column=0, sticky="w")
        ctk.CTkButton(hdr, text="⟳  Auf Updates prüfen", width=160, height=28,
                      fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"),
                      command=lambda: self._check_update(manual=True)).grid(
            row=0, column=1, padx=(0, 6))
        ctk.CTkButton(hdr, text="↻  Laden", width=90, height=28,
                      fg_color="transparent", border_width=1,
                      text_color=("gray10", "gray90"),
                      command=self._load_changelog).grid(row=0, column=2)

        self._clog_box = ctk.CTkTextbox(
            tab, font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color=COL_LOGBG, text_color=COL_LOGFG, corner_radius=6,
            state="disabled",
        )
        self._clog_box.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 8))

    # ---------------------------------------------------------- shared widgets
    def _folder_row(self, parent, row: int, label: str) -> ctk.CTkEntry:
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.grid(row=row, column=0, sticky="ew", padx=6, pady=4)
        wrap.columnconfigure(1, weight=1)
        ctk.CTkLabel(wrap, text=label, width=190, anchor="w",
                     font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, sticky="w")
        entry = ctk.CTkEntry(wrap, height=36, placeholder_text="Ordner wählen …")
        entry.grid(row=0, column=1, sticky="ew", padx=(10, 6))
        ctk.CTkButton(wrap, text="…", width=40, height=36,
                      command=lambda e=entry: self._pick_folder(e)).grid(
            row=0, column=2)
        return entry

    def _build_log_area(self) -> None:
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 14))
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(3, weight=1)

        opt = ctk.CTkFrame(wrap, fg_color="transparent")
        opt.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ctk.CTkCheckBox(
            opt, text="Backup der Originale vor jeder Aktion anlegen",
            variable=self._backup_var, command=self._on_backup_toggle,
        ).pack(side="left")

        self.progress = ctk.CTkProgressBar(wrap)
        self.progress.set(0)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(0, 8))

        ctk.CTkLabel(wrap, text="Protokoll", anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=2, column=0, sticky="w")
        self.log_widget = ctk.CTkTextbox(
            wrap, font=ctk.CTkFont(family="Consolas", size=11),
            fg_color=COL_LOGBG, text_color=COL_LOGFG, corner_radius=6,
            state="disabled",
        )
        self.log_widget.grid(row=3, column=0, sticky="nsew", pady=(2, 0))

    def _on_backup_toggle(self) -> None:
        s = _load_settings()
        s["backup_originals"] = bool(self._backup_var.get())
        _save_settings(s)

    # --------------------------------------------------------------- Actions
    def _pick_folder(self, entry: ctk.CTkEntry) -> None:
        path = filedialog.askdirectory(title="Ordner wählen")
        if path:
            entry.delete(0, "end")
            entry.insert(0, path)

    def _render_sources(self) -> None:
        for w in self._src_frame.winfo_children():
            w.destroy()
        if not self._merge_sources:
            ctk.CTkLabel(self._src_frame, text="Noch keine Archive hinzugefügt …",
                         text_color=COL_MUTED, anchor="w").grid(
                row=0, column=0, sticky="w", padx=4, pady=6)
            return
        for i, path in enumerate(self._merge_sources):
            row = ctk.CTkFrame(self._src_frame, fg_color="transparent")
            row.grid(row=i, column=0, sticky="ew", pady=2)
            row.columnconfigure(0, weight=1)
            ctk.CTkLabel(row, text=path, anchor="w").grid(
                row=0, column=0, sticky="ew", padx=(4, 6))
            ctk.CTkButton(row, text="✕", width=30, height=28,
                          fg_color=COL_DANGER, hover_color=("#991b1b", "#dc2626"),
                          command=lambda i=i: self._remove_merge_source(i)).grid(
                row=0, column=1)

    def _add_merge_source(self) -> None:
        path = filedialog.askdirectory(title="GREYHOUND-Archivordner wählen")
        if path and path not in self._merge_sources:
            self._merge_sources.append(path)
            self._render_sources()

    def _remove_merge_source(self, index: int) -> None:
        if 0 <= index < len(self._merge_sources):
            del self._merge_sources[index]
            self._render_sources()

    def _start_repair(self) -> None:
        src = self.repair_src.get().strip()
        dst = self.repair_dst.get().strip()
        if not src or not dst:
            messagebox.showwarning("Eingabe fehlt", "Bitte Quelle und Ziel angeben.")
            return
        if not self._validate_target(dst):
            return
        self._run([Path(src)], Path(dst), "Reparatur")

    def _start_merge(self) -> None:
        if len(self._merge_sources) < 2:
            messagebox.showwarning(
                "Zu wenige Archive",
                "Bitte mindestens zwei Archive zum Zusammenführen wählen.")
            return
        dst = self.merge_dst.get().strip()
        if not dst or not self._validate_target(dst):
            return
        self._run([Path(p) for p in self._merge_sources], Path(dst),
                  "Zusammenführen")

    def _validate_target(self, dst: str) -> bool:
        if (Path(dst) / "archive.db3").exists():
            messagebox.showerror(
                "Ziel nicht leer",
                "Der Zielordner enthält bereits eine archive.db3.\n"
                "Bitte einen leeren/neuen Ordner wählen, damit nichts "
                "überschrieben wird.")
            return False
        return True

    # ------------------------------------------------------------ Background
    def _run(self, sources: list[Path], target: Path, title: str) -> None:
        if self._busy:
            return
        backup = bool(self._backup_var.get())
        self._set_busy(True)
        self._clear_log()
        self.progress.set(0)
        self._log(f"=== {title} gestartet ===")
        for s in sources:
            self._log(f"  Quelle: {s}")
        self._log(f"  Ziel:   {target}")
        self._log(f"  Backup der Originale: {'ja' if backup else 'nein'}\n")
        threading.Thread(target=self._worker, args=(sources, target, title, backup),
                         daemon=True).start()

    def _worker(self, sources: list[Path], target: Path, title: str,
                backup: bool) -> None:
        try:
            report = engine.rebuild_archive(sources, target, self._log,
                                            self._set_progress, backup=backup)
            self._log("")
            self._log_report(report)
            self._log(f"=== {title} abgeschlossen ===")
            self._log_queue.put(("__DONE_OK__", None))
        except Exception as exc:  # noqa: BLE001
            self._log("")
            self._log(f"FEHLER: {exc}")
            self._log(traceback.format_exc())
            self._log_queue.put(("__DONE_ERR__", None))

    def _log_report(self, report) -> None:
        self._log("Zusammenfassung:")
        if report.backups:
            self._log(f"  Backups angelegt:    {len(report.backups)}")
            for b in report.backups:
                self._log(f"      {b}")
        self._log(f"  Items gesamt:        {report.items_total}")
        self._log(f"  davon umnummeriert:  {report.items_renumbered}")
        self._log(f"  .eml kopiert:        {report.eml_copied}")
        self._log(f"  Volltext indiziert:  {report.fts_indexed}")
        self._log(f"  Gesamtgröße:         {report.total_size:,} Bytes")
        if report.eml_missing:
            self._log(f"  ! Items ohne .eml ({len(report.eml_missing)}):")
            for m in report.eml_missing[:20]:
                self._log(f"      {m}")
            if len(report.eml_missing) > 20:
                self._log(f"      … und {len(report.eml_missing) - 20} weitere")
        if report.eml_orphaned:
            self._log(f"  i Rekonstruierte verwaiste .eml: {len(report.eml_orphaned)}")
        self._log("  Integrität Ziel:     "
                  + ("OK" if report.integrity_ok
                     else "FEHLER – " + report.integrity_msg))

    # --------------------------------------------------------------- Update
    def _check_update(self, manual: bool = False) -> None:
        threading.Thread(target=self._check_update_bg, args=(manual,),
                         daemon=True).start()

    def _check_update_bg(self, manual: bool) -> None:
        try:
            result = updater.check_for_update(updater.load_token())
            if result:
                self._log_queue.put(("__UPDATE__", result))
            elif manual:
                self._log_queue.put(("__NO_UPDATE__", None))
        except Exception as exc:  # noqa: BLE001
            if manual:
                self._log_queue.put(("__UPDATE_ERR__", str(exc)))

    def _offer_update(self, new_ver: str, asset_url: str) -> None:
        if self._busy:
            return
        if messagebox.askyesno(
            "Update verfügbar",
            f"Version {new_ver} ist verfügbar (aktuell: {__version__}).\n\n"
            "Das Tool wird aktualisiert, kurz neu gestartet und ist dann "
            "sofort wieder einsatzbereit.\n\nJetzt aktualisieren?",
        ):
            try:
                self._clog_status.set(f"Lade Version {new_ver} …")
                data = updater.download_update(updater.load_token(), asset_url)
                updater.apply_update(data)
                self.destroy()
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Update fehlgeschlagen", str(exc))

    # ------------------------------------------------------------ Changelog
    def _load_changelog(self) -> None:
        threading.Thread(target=self._load_changelog_bg, daemon=True).start()

    def _load_changelog_bg(self) -> None:
        try:
            releases = updater.fetch_releases(updater.load_token(), count=10)
            self._log_queue.put(("__CHANGELOG__", releases))
        except Exception as exc:  # noqa: BLE001
            self._log_queue.put(("__CHANGELOG_ERR__", str(exc)))

    def _render_changelog(self, releases: list) -> None:
        lines: list[str] = []
        for r in releases:
            name = r.get("name") or r.get("tag_name", "")
            pub = (r.get("published_at", "") or "")[:10]
            body = (r.get("body") or "").strip()
            if lines:
                lines.append("")
            lines.append(f"  {name}    ·    {pub}")
            lines.append("  " + "─" * 42)
            for line in (body.splitlines() if body else ["  (keine Beschreibung)"]):
                if line.strip() in ("## What's Changed", "## New Contributors"):
                    continue
                if line.startswith("**Full Changelog**"):
                    continue
                lines.append("  " + line)
        self._clog_box.configure(state="normal")
        self._clog_box.delete("1.0", "end")
        self._clog_box.insert("1.0", "\n".join(lines) or "  Keine Releases gefunden.")
        self._clog_box.configure(state="disabled")

    # ----------------------------------------------------------- UI plumbing
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.repair_btn.configure(state=state)
        self.merge_btn.configure(state=state)

    def _log(self, msg: str) -> None:
        self._log_queue.put(msg)

    def _set_progress(self, value: float) -> None:
        self._log_queue.put(("__PROGRESS__", value))

    def _clear_log(self) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.configure(state="disabled")

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                if isinstance(msg, tuple):
                    self._handle_event(*msg)
                else:
                    self.log_widget.configure(state="normal")
                    self.log_widget.insert("end", msg + "\n")
                    self.log_widget.see("end")
                    self.log_widget.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self._drain_log)

    def _handle_event(self, kind: str, payload) -> None:
        if kind == "__PROGRESS__":
            self.progress.set(float(payload))
        elif kind == "__DONE_OK__":
            self._set_busy(False)
            messagebox.showinfo("Fertig", "Vorgang erfolgreich abgeschlossen.")
        elif kind == "__DONE_ERR__":
            self._set_busy(False)
            messagebox.showerror("Fehler", "Der Vorgang ist fehlgeschlagen. "
                                 "Details im Protokoll.")
        elif kind == "__CHANGELOG__":
            self._render_changelog(payload)
            self._clog_status.set(
                f"Version {__version__}  ·  {len(payload)} Releases geladen")
        elif kind == "__CHANGELOG_ERR__":
            self._clog_status.set(f"Changelog-Fehler: {payload}")
        elif kind == "__UPDATE__":
            self._offer_update(*payload)
        elif kind == "__NO_UPDATE__":
            messagebox.showinfo("Aktuell", f"Du nutzt bereits die neueste "
                                f"Version ({__version__}).")
        elif kind == "__UPDATE_ERR__":
            messagebox.showwarning("Update-Prüfung fehlgeschlagen", str(payload))


def main() -> None:
    ctk.set_appearance_mode(
        {"System": "system", "Hell": "light", "Dunkel": "dark"}.get(
            _load_settings().get("appearance", "Dunkel"), "dark"))
    ctk.set_default_color_theme("blue")
    App().mainloop()


if __name__ == "__main__":
    main()
