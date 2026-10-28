"""Local MKVA pack/recovery tool. No library access, deletion or transcoding."""
import argparse
import contextlib
import json
import queue
import re
import shutil
import struct
import sys
import threading
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from . import mkva_format
except ImportError:  # Portable tool folder.
    try:
        import mkva_format
    except ImportError:
        import mkva_archive as mkva_format


SIDECARS = {'.nfo', '.srt', '.ass', '.ssa', '.vtt', '.sup', '.sub', '.idx',
            '.jpg', '.jpeg', '.png', '.webp', '.xml', '.txt'}


def safe_name(name):
    if (not isinstance(name, str) or not name or name in {'.', '..'}
            or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name)
            or name.endswith((' ', '.'))
            or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', name, re.I)):
        raise ValueError('Archive contains an unsafe output filename')
    return name


def regular(path):
    return path.is_file() and not path.is_symlink() and not path.is_junction()


def duration_ticks(path):
    """Read Matroska Info directly; no FFmpeg installation or codec decoding."""
    with Path(path).open('rb') as file:
        end = file.seek(0, 2)
        file.seek(0)
        while file.tell() < end:
            identifier, _ = mkva_format.vint(file, True)
            size, _ = mkva_format.vint(file)
            start = file.tell()
            stop = end if size is None else start + size
            if stop > end or stop < start:
                raise ValueError('Invalid Matroska element bounds')
            if identifier == 0x18538067:  # Segment
                end = stop
                continue
            if identifier == 0x1549a966:  # Segment Info
                scale, duration = 1_000_000, None
                while file.tell() < stop:
                    field, _ = mkva_format.vint(file, True)
                    count, _ = mkva_format.vint(file)
                    pos = file.tell()
                    if count is None or pos + count > stop:
                        raise ValueError('Invalid Matroska Info bounds')
                    if field == 0x2ad7b1 and 0 < count <= 8:
                        scale = int.from_bytes(file.read(count), 'big')
                    elif field == 0x4489 and count in {4, 8}:
                        duration = struct.unpack('>f' if count == 4 else '>d', file.read(count))[0]
                    file.seek(pos + count)
                if duration is not None:
                    ticks = round(duration * scale / 100)
                    if 0 < ticks <= 2 * 86400 * 10_000_000:
                        return ticks
                break
            file.seek(stop)
    raise ValueError(f'{Path(path).name}: valid MKV duration is missing; remux this file before archiving')


def pack_folder(folder, output):
    folder = Path(folder)
    output = Path(output)
    if folder.is_symlink() or folder.is_junction() or not folder.is_dir():
        raise ValueError('Select a regular local folder containing the edition MKVs')
    files = sorted(folder.iterdir())
    media = [p for p in files if p.suffix.lower() == '.mkv']
    if not 2 <= len(media) <= 32 or any(not regular(p) for p in media):
        raise ValueError('Select a folder with two to 32 complete, regular MKV edition files')
    if output.suffix.lower() != '.mkva':
        raise ValueError('Choose an output filename ending in .mkva')
    specs = [{'id': f'edition-{i+1}', 'label': p.stem.split(' - ', 1)[-1],
              'duration_ticks': duration_ticks(p), 'path': str(p)}
             for i, p in enumerate(media)]
    attachments = []
    for p in files:
        if p.suffix.lower() not in SIDECARS:
            continue
        if not regular(p):
            raise ValueError('Sidecars must be regular files, without links')
        safe_name(p.name)
        # Longest matching movie stem keeps each edition's subtitles together.
        matching = [(len(m.stem), spec['id']) for m, spec in zip(media, specs)
                    if p.name.casefold().startswith(m.stem.casefold())
                    and p.name[len(m.stem):len(m.stem)+1] in {'.', '-', ' '}]
        owner = max(matching)[1] if matching else None
        attachments.append({'id': f'attachment-{len(attachments)+1}', 'path': str(p),
                            'owner': owner, 'label': p.name})
    result = mkva_format.build(output, specs, attachments)
    print('Completed. Original MKVs, audio, subtitles and sidecars remain untouched.', flush=True)
    return result


def unpack(archive, destination, selected=None):
    """Recover selected editions, associated sidecars and shared artwork exactly."""
    archive = Path(archive)
    destination = Path(destination)
    catalog = mkva_format.index_of(archive)
    known = {e['id'] for e in catalog['entries']}
    chosen = known if selected is None else set(selected)
    if not chosen or not chosen.issubset(known):
        raise ValueError('Select at least one edition present in this archive')
    entries = [e for e in catalog['entries'] if e['id'] in chosen]
    entries += [e for e in catalog.get('attachments', [])
                if e.get('owner') is None or e.get('owner') in chosen]
    names = [safe_name(e['name']) for e in entries]
    if len({n.casefold() for n in names}) != len(names):
        raise ValueError('Archive has conflicting filenames; nothing extracted')
    if destination.exists():
        raise FileExistsError('Choose a new output folder; existing folders are never overwritten')
    parent = destination.parent.resolve(strict=True)
    required = sum(e['size'] for e in entries) + 256 * 1024**2
    if shutil.disk_usage(parent).free < required:
        raise ValueError(f'Extraction needs {required / 1024**3:.2f} GiB free')
    destination.mkdir()
    for e in entries:
        mkva_format.extract(archive, e['id'], destination / e['name'])
    print(f'Restored {len(chosen)} edition(s) and {len(entries)-len(chosen)} sidecars. All checksums match.', flush=True)
    return destination


def gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    root = tk.Tk()
    root.title('MKVA — Edition archive tool')
    root.geometry('960x760')
    root.minsize(880, 700)
    root.configure(background='#eef2f7')
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', font=('Segoe UI', 10), background='#ffffff', foreground='#243247')
    style.configure('App.TFrame', background='#eef2f7')
    style.configure('TButton', padding=(14, 9), background='#f5f7fb', borderwidth=1)
    style.map('TButton', background=[('active', '#e5ebf4')])
    style.configure('Primary.TButton', background='#16766d', foreground='white', font=('Segoe UI', 10, 'bold'))
    style.map('Primary.TButton', background=[('active', '#115f58'), ('disabled', '#a7b7b7')])
    style.configure('TEntry', padding=7, fieldbackground='white')
    style.configure('TNotebook', background='#eef2f7', borderwidth=0)
    style.configure('TNotebook.Tab', padding=(22, 12), background='#e3e9f1')
    style.map('TNotebook.Tab', background=[('selected', '#ffffff')], foreground=[('selected', '#126a61')])
    style.configure('Horizontal.TProgressbar', troughcolor='#dde5ee', background='#16766d', borderwidth=0)
    header = tk.Frame(root, background='#15283e', padx=28, pady=22)
    header.pack(fill='x')
    tk.Label(header, text='MKVA', background='#15283e', foreground='#6fddc6', font=('Segoe UI', 24, 'bold')).pack(anchor='w')
    tk.Label(header, text='Keep every edition. Store shared media once.', background='#15283e', foreground='#ffffff', font=('Segoe UI', 12)).pack(anchor='w', pady=(3, 5))
    tk.Label(header, text='DVD & Blu-ray MKVs  /  Original audio, subtitles and chapters', background='#15283e', foreground='#b6c6d8', font=('Segoe UI', 10)).pack(anchor='w')
    frame = ttk.Frame(root, padding=24, style='App.TFrame')
    frame.pack(fill='both', expand=True)
    tabs = ttk.Notebook(frame)
    tabs.pack(fill='x')
    create, restore = ttk.Frame(tabs, padding=18), ttk.Frame(tabs, padding=18)
    tabs.add(create, text='Create archive')
    tabs.add(restore, text='Extract editions')
    source, output, archive, destination = (tk.StringVar() for _ in range(4))
    actions, messages, busy = [], queue.Queue(), False
    catalog = {}

    def row(parent, title, variable, choose, readonly=False):
        box = ttk.Frame(parent)
        box.pack(fill='x', pady=4)
        ttk.Label(box, text=title, width=19).pack(side='left')
        ttk.Entry(box, textvariable=variable, state='readonly' if readonly else 'normal').pack(side='left', fill='x', expand=True)
        button = ttk.Button(box, text='Browse…', command=choose)
        button.pack(side='right', padx=(8, 0))
        actions.append(button)

    def choose_source():
        folder = filedialog.askdirectory(title='Select a folder containing editions of one film')
        if folder:
            source.set(folder)
            if not output.get(): output.set(str(Path(folder) / (Path(folder).name + '.mkva')))
    ttk.Label(create, text='Create a shared archive', font=('Segoe UI', 13, 'bold')).pack(anchor='w', pady=(0, 10))
    row(create, 'Editions folder', source, choose_source)
    row(create, 'New archive', output, lambda: output.set(filedialog.asksaveasfilename(
        defaultextension='.mkva', filetypes=[('MKVA archive', '*.mkva')]) or output.get()))
    ttk.Label(create, text='Choose a folder containing the versions of one film. Each MKV stays a separate edition.\nMatching NFOs, artwork and external subtitles are included. Your originals stay in place.', wraplength=800).pack(anchor='w', pady=(14, 12))

    def load():
        nonlocal catalog
        path = filedialog.askopenfilename(filetypes=[('MKVA archives', '*.mkva *.jfshare')])
        if not path:
            return
        try:
            catalog = mkva_format.index_of(path)
            archive.set(path)
            editions.delete(0, 'end')
            for e in catalog['entries']:
                seconds = e.get('duration_ticks', 0) / 10_000_000
                runtime = f'{int(seconds//3600)}h {int(seconds%3600//60):02d}m' if seconds else 'Duration unavailable'
                editions.insert('end', f"  {e.get('label', e['name'])}   |   {runtime}   |   {e['size']/1024**3:.2f} GiB")
            editions.selection_set(0, 'end')
        except Exception as error:
            catalog = {}
            editions.delete(0, 'end')
            messagebox.showerror('Cannot open archive', str(error))

    ttk.Label(restore, text='Restore standalone editions', font=('Segoe UI', 13, 'bold')).pack(anchor='w', pady=(0, 10))
    row(restore, 'Archive', archive, load, readonly=True)
    row(restore, 'New output folder', destination, lambda: destination.set(
        str(Path(p) / 'Extracted editions') if (p := filedialog.askdirectory(title='Choose parent for a NEW extraction folder')) else destination.get()))
    editions = tk.Listbox(restore, selectmode='extended', height=4, exportselection=False,
                          font=('Segoe UI', 10), background='#f5f8fc', foreground='#243247',
                          selectbackground='#d6eee8', selectforeground='#125a51',
                          borderwidth=0, highlightthickness=1, highlightbackground='#d7e1ec')
    editions.pack(fill='x', pady=5)
    ttk.Label(restore, text='Select editions with Ctrl-click. Audio, subtitles and chapters return exactly as stored.').pack(anchor='w', pady=5)
    progress = ttk.Progressbar(frame, mode='indeterminate')
    progress.pack(fill='x', pady=(12, 4))
    status = tk.StringVar(value='Ready')
    tk.Label(frame, textvariable=status, background='#eef2f7', foreground='#405570', font=('Segoe UI', 10)).pack(anchor='w', pady=4)
    log = tk.Text(frame, height=7, wrap='word', state='disabled', font=('Segoe UI', 10),
                  background='#ffffff', foreground='#405570', borderwidth=0, padx=12, pady=10)
    log.pack(fill='both', expand=True, pady=(6, 0))

    class Writer:
        def write(self, data):
            if not data.strip(): return
            try:
                item = json.loads(data)
                if 'percent' in item:
                    line = f"Packing {item['cut']} · {item['percent']}% · {item['shared_GiB']:.2f} GiB shared"
                elif item.get('indexed'): line = f"Stored {item['cut']}"
                elif item.get('byte_exact_reconstruction_verified'): line = f"Checksum verified: {item['cut']}"
                elif 'restored' in item: line = f"Restored and verified: {Path(item['restored']).name}"
                elif 'archive_bytes' in item: line = f"Archive complete: {item['archive_bytes']/1024**3:.2f} GiB"
                else: line = data
            except (ValueError, TypeError): line = data
            messages.put(('log', line))
        def flush(self): pass

    def run(work):
        nonlocal busy
        if busy: return
        busy = True
        for button in actions: button.state(['disabled'])
        editions.configure(state='disabled')
        progress.start()
        status.set('Working — originals are preserved. Large files can take several minutes.')
        def worker():
            try:
                with contextlib.redirect_stdout(Writer()): work()
                messages.put(('done', 'Complete — all checksums verified.'))
            except Exception as error: messages.put(('error', str(error)))
        threading.Thread(target=worker, daemon=True).start()

    def create_clicked():
        folder, target = source.get(), output.get()
        if not folder or not target: return messagebox.showerror('Choose paths', 'Choose the editions folder and new archive filename.')
        movies = sorted(Path(folder).glob('*.mkv'))
        if not messagebox.askokcancel('Create this archive?', 'Include these editions?\n\n' + '\n'.join(p.name for p in movies)):
            return
        run(lambda: pack_folder(folder, target))

    def extract_clicked():
        target, path = destination.get(), archive.get()
        chosen = [catalog['entries'][i]['id'] for i in editions.curselection()] if catalog else []
        if not target or not path or not chosen: return messagebox.showerror('Choose editions', 'Open an archive, select editions and choose a new output folder.')
        run(lambda: unpack(path, target, chosen))

    for parent, label, command in ((create, 'Create verified MKVA', create_clicked), (restore, 'Extract selected editions', extract_clicked)):
        button = ttk.Button(parent, text=label, command=command, style='Primary.TButton')
        button.pack(anchor='e', pady=(8, 0))
        actions.append(button)

    def poll():
        nonlocal busy
        while not messages.empty():
            kind, text = messages.get_nowait()
            log.configure(state='normal')
            log.insert('end', text + '\n')
            log.see('end')
            log.configure(state='disabled')
            if kind != 'log':
                busy = False
                progress.stop()
                status.set(text if kind == 'done' else 'Stopped — originals and any partial output are preserved.')
                for button in actions: button.state(['!disabled'])
                editions.configure(state='normal')
                if kind == 'error': messagebox.showerror('Operation stopped', text)
        root.after(200, poll)

    def close():
        if busy:
            messagebox.showinfo('Operation running', 'Please let the operation finish. Originals are preserved; closing now would leave partial output.')
        else: root.destroy()
    root.protocol('WM_DELETE_WINDOW', close)
    poll()
    root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('gui')
    p = sub.add_parser('pack'); p.add_argument('folder'); p.add_argument('output')
    p = sub.add_parser('list'); p.add_argument('archive')
    p = sub.add_parser('extract'); p.add_argument('archive'); p.add_argument('destination'); p.add_argument('--edition', action='append')
    args = parser.parse_args(argv)
    if args.command in {None, 'gui'}: gui()
    elif args.command == 'pack': pack_folder(args.folder, args.output)
    elif args.command == 'list': print(json.dumps(mkva_format.index_of(args.archive), indent=2))
    else: unpack(args.archive, args.destination, args.edition)


if __name__ == '__main__':
    main()
