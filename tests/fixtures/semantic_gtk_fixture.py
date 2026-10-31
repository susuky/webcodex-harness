"""Disposable GTK app for the isolated semantic desktop acceptance check."""

import json
import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

output = Path(sys.argv[1])
window = Gtk.Window(title=f"WebCodex Semantic Fixture {os.getpid()}")
window.set_default_size(420, 260)
window.connect("destroy", Gtk.main_quit)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
window.add(box)
entry = Gtk.Entry()
entry.get_accessible().set_name("Message")
box.pack_start(entry, False, False, 0)
password = Gtk.Entry()
password.set_visibility(False)
password.set_text("fixture-secret-never-return")
password.get_accessible().set_name("Password")
box.pack_start(password, False, False, 0)
count = 0


def save(button):
    global count
    count += 1
    output.write_text(json.dumps({"count": count, "text": entry.get_text()}), encoding="utf-8")


def move(button):
    window.move(460, 340)
    window.resize(630, 340)


for name, callback in [
    ("Apply", save),
    ("Move and resize", move),
    ("Duplicate", save),
    ("Duplicate", save),
]:
    button = Gtk.Button(label=name)
    button.connect("clicked", callback)
    box.pack_start(button, False, False, 0)
window.show_all()
print(json.dumps({"pid": os.getpid(), "name": window.get_title()}), flush=True)
Gtk.main()
