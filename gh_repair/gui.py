"""tkinter-Oberflaeche fuer das GREYHOUND Archive Repair Tool."""

from __future__ import annotations

import queue
import threading
import traceback
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    HORIZONTAL,
    LEFT,
    RIGHT,
    Tk,
    X,
    Y,
    filedialog,
    messagebox,
)
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__, core, engine, updater
from .core import Archive, Report


class App:
    def __init__(self, root: Tk) -> None:
        self.root = root
        root.title(f"GREYHOUND Archive Repair Tool v{__version__}")
        root.geometry("780x620")
        root.minsize(680, 540)

        self._log_queue: queue.Queue[str] = queue.Queue()
        self._busy = False

        notebook = ttk.Notebook(root)
        notebook.pack(fill=X, padx=10, pady=(10, 0))
        self._build_repair_tab(notebook)
        self._build_merge_tab(notebook)
        self._build_changelog_tab(notebook)

        # gemeinsame Fortschritts- und Log-Anzeige
        self.progress = ttk.Progressbar(root, mode="determinate", maximum=1.0)
        self.progress.pack(fill=X, padx=10, pady=(10, 4))

        ttk.Label(root, text="Protokoll:").pack(anchor="w", padx=10)
        self.log_widget = ScrolledText(root, height=16, state="disabled",
                                       font=("Consolas", 9))
        self.log_widget.pack(fill=BOTH, expand=True, padx=10, pady=(0, 10))

        self.root.after(120, self._drain_log)
        # Update-Check nach 3 Sek. (nicht-blockierend, im Hintergrund)
        self.root.after(3000, self._check_update)

    # ------------------------------------------------------------------ Tabs
    def _build_repair_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=12)
        nb.add(tab, text="  Reparieren  ")

        ttk.Label(
            tab,
            text="Repariert eine einzelne (defekte) archive.db3: Index neu "
            "aufbauen,\nStatistik neu berechnen, defekte DB wiederherstellen, "
            "Datei↔DB abgleichen.",
            justify=LEFT,
        ).pack(anchor="w", pady=(0, 10))

        self.repair_src = self._folder_row(tab, "Defektes Archiv:")
        self.repair_dst = self._folder_row(tab, "Ziel (Reparatur-Kopie):")

        self.repair_btn = ttk.Button(
            tab, text="Reparatur starten", command=self._start_repair
        )
        self.repair_btn.pack(anchor="e", pady=(8, 0))

    def _build_merge_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=12)
        nb.add(tab, text="  Zusammenführen  ")

        ttk.Label(
            tab,
            text="Führt mehrere Archive verlustfrei zu einem zusammen. "
            "Kollidierende\nItem-IDs werden neu nummeriert, .eml umbenannt und "
            "alle Verweise mitgezogen.",
            justify=LEFT,
        ).pack(anchor="w", pady=(0, 10))

        listframe = ttk.Frame(tab)
        listframe.pack(fill=X)
        self.merge_list = ttk.Treeview(
            listframe, columns=("path",), show="tree", height=6
        )
        self.merge_list.pack(side=LEFT, fill=BOTH, expand=True)
        sb = ttk.Scrollbar(listframe, orient="vertical",
                           command=self.merge_list.yview)
        sb.pack(side=RIGHT, fill=Y)
        self.merge_list.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(tab)
        btns.pack(fill=X, pady=(6, 10))
        ttk.Button(btns, text="Archiv hinzufügen …",
                   command=self._add_merge_source).pack(side=LEFT)
        ttk.Button(btns, text="Entfernen",
                   command=self._remove_merge_source).pack(side=LEFT, padx=6)

        self.merge_dst = self._folder_row(tab, "Ziel (neues Gesamtarchiv):")

        self.merge_btn = ttk.Button(
            tab, text="Zusammenführen starten", command=self._start_merge
        )
        self.merge_btn.pack(anchor="e", pady=(8, 0))

    def _build_changelog_tab(self, nb: ttk.Notebook) -> None:
        tab = ttk.Frame(nb, padding=12)
        nb.add(tab, text="  Changelog  ")

        hdr = ttk.Frame(tab)
        hdr.pack(fill=X)
        self.clog_status = ttk.Label(hdr, text=f"Version {__version__}")
        self.clog_status.pack(side=LEFT)
        ttk.Button(hdr, text="Auf Updates prüfen",
                   command=lambda: self._check_update(manual=True)).pack(side=RIGHT)
        ttk.Button(hdr, text="↻ Changelog laden",
                   command=self._load_changelog).pack(side=RIGHT, padx=6)

        self.clog_box = ScrolledText(tab, height=14, state="disabled",
                                     font=("Consolas", 9))
        self.clog_box.pack(fill=BOTH, expand=True, pady=(8, 0))
        self.root.after(1500, self._load_changelog)

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
            lines.append(f"  {name}   ·   {pub}")
            lines.append("  " + "─" * 40)
            for line in (body.splitlines() if body else ["  (keine Beschreibung)"]):
                if line.strip() in ("## What's Changed", "## New Contributors"):
                    continue
                if line.startswith("**Full Changelog**"):
                    continue
                lines.append("  " + line)
        self.clog_box.configure(state="normal")
        self.clog_box.delete("1.0", END)
        self.clog_box.insert("1.0", "\n".join(lines) or "  Keine Releases gefunden.")
        self.clog_box.configure(state="disabled")

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
                self.clog_status["text"] = f"Lade Version {new_ver} …"
                data = updater.download_update(updater.load_token(), asset_url)
                updater.apply_update(data)
                self.root.destroy()
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Update fehlgeschlagen", str(exc))

    def _folder_row(self, parent: ttk.Frame, label: str) -> ttk.Entry:
        row = ttk.Frame(parent)
        row.pack(fill=X, pady=3)
        ttk.Label(row, text=label, width=24).pack(side=LEFT)
        entry = ttk.Entry(row)
        entry.pack(side=LEFT, fill=X, expand=True)
        ttk.Button(
            row, text="Durchsuchen …",
            command=lambda e=entry: self._pick_folder(e),
        ).pack(side=LEFT, padx=(6, 0))
        return entry

    # --------------------------------------------------------------- Actions
    def _pick_folder(self, entry: ttk.Entry) -> None:
        path = filedialog.askdirectory(title="Ordner wählen")
        if path:
            entry.delete(0, END)
            entry.insert(0, path)

    def _add_merge_source(self) -> None:
        path = filedialog.askdirectory(title="GREYHOUND-Archivordner wählen")
        if not path:
            return
        existing = {self.merge_list.item(i, "text") for i in self.merge_list.get_children()}
        if path not in existing:
            self.merge_list.insert("", END, text=path)

    def _remove_merge_source(self) -> None:
        for item in self.merge_list.selection():
            self.merge_list.delete(item)

    def _start_repair(self) -> None:
        src = self.repair_src.get().strip()
        dst = self.repair_dst.get().strip()
        if not src or not dst:
            messagebox.showwarning("Eingabe fehlt",
                                   "Bitte Quelle und Ziel angeben.")
            return
        if not self._validate_target(dst):
            return
        self._run([Path(src)], Path(dst), "Reparatur")

    def _start_merge(self) -> None:
        sources = [
            Path(self.merge_list.item(i, "text"))
            for i in self.merge_list.get_children()
        ]
        dst = self.merge_dst.get().strip()
        if len(sources) < 2:
            messagebox.showwarning(
                "Zu wenige Archive",
                "Bitte mindestens zwei Archive zum Zusammenführen wählen.",
            )
            return
        if not dst or not self._validate_target(dst):
            return
        self._run(sources, Path(dst), "Zusammenführen")

    def _validate_target(self, dst: str) -> bool:
        if (Path(dst) / "archive.db3").exists():
            messagebox.showerror(
                "Ziel nicht leer",
                "Der Zielordner enthält bereits eine archive.db3.\n"
                "Bitte einen leeren/neuen Ordner wählen, damit nichts "
                "überschrieben wird.",
            )
            return False
        return True

    # ------------------------------------------------------------ Background
    def _run(self, sources: list[Path], target: Path, title: str) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self._clear_log()
        self.progress["value"] = 0
        self._log(f"=== {title} gestartet ===")
        for s in sources:
            self._log(f"  Quelle: {s}")
        self._log(f"  Ziel:   {target}")
        self._log("")

        thread = threading.Thread(
            target=self._worker, args=(sources, target, title), daemon=True
        )
        thread.start()

    def _worker(self, sources: list[Path], target: Path, title: str) -> None:
        try:
            report = engine.rebuild_archive(
                sources, target, self._log, self._set_progress
            )
            self._log("")
            self._log_report(report)
            self._log(f"=== {title} abgeschlossen ===")
            self._log_queue.put("__DONE_OK__")
        except Exception as exc:  # noqa: BLE001
            self._log("")
            self._log(f"FEHLER: {exc}")
            self._log(traceback.format_exc())
            self._log_queue.put("__DONE_ERR__")

    def _log_report(self, report: Report) -> None:
        self._log("Zusammenfassung:")
        self._log(f"  Items gesamt:        {report.items_total}")
        self._log(f"  davon umnummeriert:  {report.items_renumbered}")
        self._log(f"  .eml kopiert:        {report.eml_copied}")
        self._log(f"  Volltext indiziert:  {report.fts_indexed}")
        self._log(f"  Gesamtgroesse:       {report.total_size:,} Bytes")
        if report.eml_missing:
            self._log(f"  ! Items ohne .eml ({len(report.eml_missing)}):")
            for m in report.eml_missing[:20]:
                self._log(f"      {m}")
            if len(report.eml_missing) > 20:
                self._log(f"      … und {len(report.eml_missing) - 20} weitere")
        if report.eml_orphaned:
            self._log(f"  i Rekonstruierte verwaiste .eml: {len(report.eml_orphaned)}")
        self._log(
            "  Integritaet Ziel:    "
            + ("OK" if report.integrity_ok else "FEHLER – " + report.integrity_msg)
        )

    # ----------------------------------------------------------- UI plumbing
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.repair_btn["state"] = state
        self.merge_btn["state"] = state

    def _log(self, msg: str) -> None:
        self._log_queue.put(msg)

    def _set_progress(self, value: float) -> None:
        self._log_queue.put(f"__PROGRESS__{value}")

    def _clear_log(self) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", END)
        self.log_widget.configure(state="disabled")

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self._log_queue.get_nowait()
                if isinstance(msg, tuple):
                    self._handle_event(*msg)
                    continue
                if msg.startswith("__PROGRESS__"):
                    self.progress["value"] = float(msg[len("__PROGRESS__"):])
                elif msg == "__DONE_OK__":
                    self._set_busy(False)
                    messagebox.showinfo("Fertig", "Vorgang erfolgreich abgeschlossen.")
                elif msg == "__DONE_ERR__":
                    self._set_busy(False)
                    messagebox.showerror(
                        "Fehler", "Der Vorgang ist fehlgeschlagen. "
                        "Details im Protokoll."
                    )
                else:
                    self.log_widget.configure(state="normal")
                    self.log_widget.insert(END, msg + "\n")
                    self.log_widget.see(END)
                    self.log_widget.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log)

    def _handle_event(self, kind: str, payload) -> None:
        if kind == "__CHANGELOG__":
            self._render_changelog(payload)
            self.clog_status["text"] = (
                f"Version {__version__}  ·  {len(payload)} Releases geladen"
            )
        elif kind == "__CHANGELOG_ERR__":
            self.clog_status["text"] = f"Changelog-Fehler: {payload}"
        elif kind == "__UPDATE__":
            self._offer_update(*payload)
        elif kind == "__NO_UPDATE__":
            messagebox.showinfo("Aktuell",
                                f"Du nutzt bereits die neueste Version "
                                f"({__version__}).")
        elif kind == "__UPDATE_ERR__":
            messagebox.showwarning("Update-Prüfung fehlgeschlagen", str(payload))


def main() -> None:
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
